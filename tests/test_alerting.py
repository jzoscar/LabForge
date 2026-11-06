from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import write_env
from labforge import alerting
from labforge.iac.loader import LoadError, load_path, load_policies
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import AlertPolicyDoc, Channel
from labforge.main import create_app
from labforge.models import Alert, DeviceHealth, NotificationLog, ResourceState, utcnow
from labforge.notify import ChannelError, Item, Notification, send

SECRETS = SecretResolver(environ={
    "LABFORGE_SECRET_SLACK": "https://hooks.slack.test/T1", "LABFORGE_SECRET_TEAMS": "https://teams.test/hook",
    "LABFORGE_SECRET_TWILIO": "twilio-token", "LABFORGE_SECRET_META": "meta-token", "LABFORGE_SECRET_PD": "pd-key",
    "LABFORGE_SECRET_OG": "og-key",
})

POLICY = {
    "apiVersion": "labforge/v1", "kind": "AlertPolicy", "metadata": {"name": "p"},
    "spec": {
        "channels": [
            {"name": "slack", "type": "slack", "urlRef": "slack"},
            {"name": "pager", "type": "pagerduty", "routingKeyRef": "pd"},
            {"name": "wa", "type": "whatsapp", "provider": "twilio", "accountSid": "AC1", "tokenRef": "twilio",
             "from": "+15550001", "to": ["+15550002"]},
        ],
        "routes": [
            {"match": {"tier": ["staging", "prod"], "severity": ["critical"]}, "channels": ["pager", "wa", "slack"]},
            {"match": {"kind": ["pool"]}, "channels": ["wa"], "continue": True},
            {"channels": ["slack"]},
        ],
        "repeatAfterMinutes": 30,
    },
}


class Capture:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


@pytest.fixture
def capture():
    cap = Capture()
    return cap, httpx.Client(transport=httpx.MockTransport(cap))


def note(*items: Item) -> Notification:
    return Notification(list(items), "http://lf.test")


ITEM = Item(7, "staging.host.a:status", "staging.host.a", "critical", "down: tcp:22 refused", "staging", "staging", "host")


# --- channels -------------------------------------------------------------------


def test_slack_message_has_buttons(capture):
    cap, client = capture
    send(Channel(name="s", type="slack", url_ref="slack"), note(ITEM), SECRETS, client)
    body = json.loads(cap.requests[0].content)
    assert str(cap.requests[0].url) == "https://hooks.slack.test/T1"
    assert body["blocks"][0]["text"]["text"] == "[LabForge] 1 firing"
    actions = [b for b in body["blocks"] if b["type"] == "actions"][0]["elements"]
    assert [a["action_id"] for a in actions] == ["ack", "silence_1h"] and actions[0]["value"] == "7"


def test_teams_card_and_webhook(capture):
    cap, client = capture
    send(Channel(name="t", type="teams", url_ref="teams"), note(ITEM), SECRETS, client)
    card = json.loads(cap.requests[0].content)["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and "staging.host.a" in card["body"][1]["text"]
    send(Channel(name="w", type="webhook", url="http://hooks.test/x"), note(ITEM), SECRETS, client)
    assert json.loads(cap.requests[1].content)["alerts"][0]["fingerprint"] == "staging.host.a:status"


def test_whatsapp_twilio_meta_and_sms(capture):
    cap, client = capture
    send(Channel(name="w", type="whatsapp", account_sid="AC1", token_ref="twilio", sender="+1555", to=["+1666"]),
         note(ITEM), SECRETS, client)
    form = parse_qs(cap.requests[0].content.decode())
    assert cap.requests[0].url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert form["From"] == ["whatsapp:+1555"] and form["To"] == ["whatsapp:+1666"]
    assert cap.requests[0].headers["authorization"].startswith("Basic ")

    send(Channel(name="m", type="whatsapp", provider="meta", phone_number_id="123", token_ref="meta", to=["+1666"],
                 template="lab_alert"), note(ITEM), SECRETS, client)
    meta = json.loads(cap.requests[1].content)
    assert cap.requests[1].url.path == "/v20.0/123/messages" and meta["to"] == "1666" and meta["type"] == "template"
    assert cap.requests[1].headers["authorization"] == "Bearer meta-token"

    many = [Item(i, f"f{i}", f"host{i}", "warning", "degraded") for i in range(8)]
    send(Channel(name="s", type="sms", account_sid="AC1", token_ref="twilio", sender="+1555", to=["+1666"]),
         note(*many), SECRETS, client)
    body = parse_qs(cap.requests[2].content.decode())["Body"][0]
    assert body.endswith("+3 more") and parse_qs(cap.requests[2].content.decode())["From"] == ["+1555"]


def test_pagerduty_and_opsgenie_trigger_and_resolve(capture):
    cap, client = capture
    resolved = Item(7, "staging.host.a:status", "staging.host.a", "critical", "back", resolved=True)
    send(Channel(name="p", type="pagerduty", routing_key_ref="pd"), note(ITEM, resolved), SECRETS, client)
    events = [json.loads(r.content) for r in cap.requests]
    assert [e["event_action"] for e in events] == ["trigger", "resolve"]
    assert events[0]["dedup_key"] == events[1]["dedup_key"] == "staging.host.a:status"
    assert events[0]["payload"]["severity"] == "critical"

    send(Channel(name="o", type="opsgenie", api_key_ref="og"), note(ITEM, resolved), SECRETS, client)
    assert cap.requests[2].url.path == "/v2/alerts" and json.loads(cap.requests[2].content)["priority"] == "P1"
    assert cap.requests[3].url.path == "/v2/alerts/staging.host.a:status/close"
    assert cap.requests[3].headers["authorization"] == "GenieKey og-key"


def test_missing_secret_is_a_channel_error(capture):
    _, client = capture
    with pytest.raises(ChannelError, match="not set"):
        send(Channel(name="s", type="slack", url_ref="nope"), note(ITEM), SECRETS, client)


# --- policy -----------------------------------------------------------------------


def test_policy_validation(tmp_path):
    assert AlertPolicyDoc.model_validate(POLICY).spec.routes[1].continue_
    incomplete = json.loads(json.dumps(POLICY))
    incomplete["spec"]["channels"].append({"name": "x", "type": "sms", "to": ["+1"]})
    with pytest.raises(LoadError, match="sms channel needs accountSid, tokenRef, from"):
        load_path(write_env(tmp_path / "a.yaml", json.dumps(incomplete)))
    dangling = json.loads(json.dumps(POLICY))
    dangling["spec"]["routes"][0]["channels"] = ["ghost"]
    with pytest.raises(LoadError, match="route 1: unknown channel 'ghost'"):
        load_path(write_env(tmp_path / "c.yaml", json.dumps(dangling)))
    with pytest.raises(LoadError, match="kind: expected one of Environment, AlertPolicy"):
        load_path(write_env(tmp_path / "b.yaml", "apiVersion: labforge/v1\nkind: Gizmo\n"))


def test_example_policy_loads():
    from conftest import ROOT

    [policy] = load_policies(ROOT / "infra")
    assert {c.type for c in policy.spec.channels} == {"slack", "teams", "whatsapp", "sms", "pagerduty"}


# --- engine -------------------------------------------------------------------------


def _state(session, address, status, tier="staging", labels=None, detail="tcp:22 refused"):
    env, kind, name = address.split(".")
    session.add(ResourceState(address=address, env=env, tier=tier, kind=kind, name=name, status=status,
                              status_detail=detail, spec_hash="x",
                              spec_json=json.dumps({"name": name, "platform": {"os": "linux"}, "labels": labels or {}})))
    session.commit()


def _set(session, address, status):
    st = session.exec(select(ResourceState).where(ResourceState.address == address)).one()
    st.status = status
    session.add(st)
    session.commit()


def test_route_dedupe_repeat_ack_resolve(engine, capture):
    cap, client = capture
    policies = [AlertPolicyDoc.model_validate(POLICY)]
    with Session(engine) as s:
        _state(s, "staging.host.a", "down")
        _state(s, "dev.host.b", "degraded", tier="dev")
        now = utcnow()
        r = alerting.evaluate(s, policies, base_url="http://lf", secrets=SECRETS, client=client, now=now)
        assert r == {"firing": 2, "notified": 2, "sent": 3, "failed": 0}
        # staging critical -> pager + wa + slack; dev warning -> slack; batched: one slack message
        assert sorted(cap.hosts()) == ["api.twilio.com", "events.pagerduty.com", "hooks.slack.test"]
        slack = json.loads([r for r in cap.requests if r.url.host == "hooks.slack.test"][0].content)
        assert slack["blocks"][0]["text"]["text"] == "[LabForge] 2 firing"

        cap.requests.clear()
        assert alerting.evaluate(s, policies, base_url="x", secrets=SECRETS, client=client, now=now)["notified"] == 0

        later = now + timedelta(minutes=31)
        dev = s.exec(select(Alert).where(Alert.subject == "dev.host.b")).one()
        alerting.acknowledge(s, dev.id, "kun")
        r = alerting.evaluate(s, policies, base_url="x", secrets=SECRETS, client=client, now=later)
        assert r["notified"] == 1  # only the unacknowledged staging alert repeats

        cap.requests.clear()
        _set(s, "staging.host.a", "up")
        r = alerting.evaluate(s, policies, base_url="x", secrets=SECRETS, client=client, now=later)
        resolve = json.loads([r for r in cap.requests if r.url.host == "events.pagerduty.com"][0].content)
        assert resolve["event_action"] == "resolve"
        a = s.exec(select(Alert).where(Alert.subject == "staging.host.a")).one()
        assert a.status == "resolved" and a.resolve_notified
        assert s.exec(select(NotificationLog)).first().ok


def test_silence_escalation_and_rig_alerts(engine, capture):
    cap, client = capture
    policies = [AlertPolicyDoc.model_validate(POLICY)]
    with Session(engine) as s:
        _state(s, "staging.host.a", "degraded")
        alerting.silence(s, "staging.*", 60, "maintenance", "kun")
        assert alerting.evaluate(s, policies, base_url="x", secrets=SECRETS, client=client)["notified"] == 0
        assert s.exec(select(Alert)).one().status == "firing"  # recorded, just quiet

        _state(s, "test.host.rig1", "up", tier="test", labels={"pool": "p"})
        _state(s, "test.host.rig2", "up", tier="test", labels={"pool": "p"})
        s.add(DeviceHealth(address="test.host.rig1", quarantined=True, reason="overheat"))
        s.add(DeviceHealth(address="test.host.rig2", quarantined=True, reason="overheat"))
        s.commit()
        alerting.evaluate(s, policies, base_url="x", secrets=SECRETS, client=client)
        subjects = {a.subject: a for a in s.exec(select(Alert))}
        assert subjects["pool:p"].severity == "critical" and subjects["pool:p"].kind == "pool"
        assert "quarantined: overheat" in subjects["test.host.rig1"].summary
        assert "api.twilio.com" in cap.hosts()  # pool route -> whatsapp (continue) -> then slack


def test_slack_buttons_and_metrics(settings, engine):
    settings.slack_signing_secret = "shh"
    with Session(engine) as s:
        _state(s, "staging.host.a", "down")
        alerting.evaluate(s, [], base_url="x")
        alert_id = s.exec(select(Alert)).one().id
    payload = {"actions": [{"action_id": "ack", "value": str(alert_id)}], "user": {"username": "kun"}}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"shh", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    with TestClient(create_app(settings)) as c:
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Slack-Request-Timestamp": ts}
        assert c.post("/api/v1/alerts/slack/actions", content=body,
                      headers={**headers, "X-Slack-Signature": "v0=bad"}).status_code == 401
        r = c.post("/api/v1/alerts/slack/actions", content=body, headers={**headers, "X-Slack-Signature": sig})
        assert r.json()["text"] == "staging.host.a acknowledged by kun"
        assert c.get("/api/v1/alerts").json()[0]["acked_by"] == "slack:kun"

        metrics = c.get("/metrics").text
        assert 'labforge_resource_status{address="staging.host.a",env="staging",tier="staging",kind="host",status="down"} 1' in metrics
        assert 'labforge_alerts_firing{severity="critical"} 1' in metrics
        page = c.get("/alerts").text
        assert "staging.host.a" in page and "Availability, last 7 days" in page
