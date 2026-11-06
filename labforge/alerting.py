"""Alerts: derived from monitoring and rig state each sweep, routed by the AlertPolicy in IaC.

- One alert per condition (fingerprint), so a flapping host does not create a pile of alerts.
- Notifications are batched: one message per channel per sweep, however many alerts changed.
- Unacknowledged alerts are re-sent after `repeatAfterMinutes`; acknowledged ones stay quiet.
- Silences mute matching alerts (glob on the subject) without hiding them from the Alerts page.
- A resolved alert sends one recovery notice to the channels that heard about it.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import AlertPolicyDoc, Channel
from labforge.models import Alert, DeviceHealth, Event, NotificationLog, ResourceState, Silence, utcnow
from labforge.notify import ChannelError, Item, Notification, send

log = logging.getLogger("labforge.alerting")
RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Condition:
    fingerprint: str
    subject: str
    severity: str
    summary: str
    env: str = ""
    tier: str = ""
    kind: str = ""
    labels: dict | None = None


def conditions(session: Session, pool_min_available: float = 0.5) -> dict[str, Condition]:
    from labforge.rigs import overview  # rigs imports the monitor, which imports this module lazily

    out: dict[str, Condition] = {}
    states = list(session.exec(select(ResourceState)))
    for st in states:
        if st.status in ("down", "degraded"):
            out[f"{st.address}:status"] = Condition(
                f"{st.address}:status", st.address, "critical" if st.status == "down" else "warning",
                f"{st.status}: {st.status_detail or 'see LabForge'}", st.env, st.tier, st.kind, st.spec.get("labels", {}))
    by_address = {s.address: s for s in states}
    for row in session.exec(select(DeviceHealth).where(DeviceHealth.quarantined == True)):  # noqa: E712
        st = by_address.get(row.address)
        out[f"{row.address}:quarantine"] = Condition(
            f"{row.address}:quarantine", row.address, "warning", f"quarantined: {row.reason}",
            st.env if st else "", st.tier if st else "", "host", st.spec.get("labels", {}) if st else {})
    for pool in overview(session):
        devices = pool["devices"]
        usable = sum(1 for d in devices if d["rig"] != "quarantined" and d["state"].status == "up")
        if devices and usable / len(devices) < pool_min_available:
            first = devices[0]["state"]
            out[f"pool:{pool['pool']}:capacity"] = Condition(
                f"pool:{pool['pool']}:capacity", f"pool:{pool['pool']}", "critical" if usable == 0 else "warning",
                f"only {usable} of {len(devices)} devices usable", first.env, first.tier, "pool", {})
    return out


def _matches(route_match, alert: Alert) -> bool:
    labels = json.loads(alert.labels_json or "{}")
    m = route_match
    return ((not m.env or alert.env in m.env) and (not m.tier or alert.tier in m.tier)
            and (not m.kind or alert.kind in m.kind) and (not m.severity or alert.severity in m.severity)
            and all(labels.get(k) == v for k, v in m.labels.items()))


def route(policies: list[AlertPolicyDoc], alert: Alert) -> list[Channel]:
    channels: list[Channel] = []
    for policy in policies:
        by_name = {c.name: c for c in policy.spec.channels}
        for r in policy.spec.routes:
            if _matches(r.match, alert):
                channels += [by_name[n] for n in r.channels if by_name[n] not in channels]
                if not r.continue_:
                    break
    return channels


def silenced(subject: str, silences: list[Silence]) -> bool:
    return any(fnmatch.fnmatchcase(subject, s.matcher) for s in silences)


def evaluate(session: Session, policies: list[AlertPolicyDoc], *, base_url: str, now: datetime | None = None,
             secrets: SecretResolver | None = None, client: httpx.Client | None = None) -> dict:
    """Update alerts from current state and send what changed. Returns counts for logging/tests."""
    now = now or utcnow()
    secrets = secrets or SecretResolver()
    min_available = min((p.spec.pool_min_available for p in policies), default=0.5)
    repeat = timedelta(minutes=min((p.spec.repeat_after_minutes for p in policies), default=240))
    current = conditions(session, min_available)
    rows = {a.fingerprint: a for a in session.exec(select(Alert))}
    silences = list(session.exec(select(Silence).where(Silence.until > now)))

    outgoing: list[tuple[Alert, Item]] = []
    for fp, c in current.items():
        a = rows.get(fp)
        if a is None or a.status == "resolved":
            a = a or Alert(fingerprint=fp, subject=c.subject)
            a.status, a.started_at, a.resolved_at = "firing", now, None
            a.acked_by = a.acked_at = a.notified_at = None
            a.resolve_notified = False
        escalated = a.notified_at is not None and RANK[c.severity] > RANK.get(a.severity, 0)
        a.severity, a.summary, a.last_seen = c.severity, c.summary[:500], now
        a.env, a.tier, a.kind, a.labels_json = c.env, c.tier, c.kind, json.dumps(c.labels or {})
        session.add(a)
        session.flush()
        due_repeat = a.notified_at is not None and not a.acked_by and now - a.notified_at >= repeat
        if (a.notified_at is None or escalated or due_repeat) and not silenced(a.subject, silences):
            outgoing.append((a, Item(a.id or 0, fp, a.subject, a.severity, a.summary, a.env, a.tier, a.kind,
                                     repeat=due_repeat and not escalated)))

    for fp, a in rows.items():
        if a.status == "firing" and fp not in current:
            a.status, a.resolved_at = "resolved", now
            session.add(a)
            if a.notified_at is not None and not a.resolve_notified and not silenced(a.subject, silences):
                outgoing.append((a, Item(a.id or 0, fp, a.subject, a.severity, "back to normal", a.env, a.tier,
                                         a.kind, resolved=True)))
    session.commit()

    sent = failed = 0
    if outgoing and policies:
        per_channel: dict[str, tuple[Channel, list[tuple[Alert, Item]]]] = {}
        for a, item in outgoing:
            for ch in route(policies, a):
                per_channel.setdefault(ch.name, (ch, []))[1].append((a, item))
        own_client = client is None
        client = client or httpx.Client()
        try:
            for name, (ch, entries) in per_channel.items():
                note = Notification([item for _, item in entries], base_url)
                try:
                    send(ch, note, secrets, client)
                    ok, detail = True, note.title
                    sent += 1
                except ChannelError as e:
                    ok, detail = False, str(e)
                    failed += 1
                    log.warning("notification to %s failed: %s", name, e)
                session.add(NotificationLog(channel=name, channel_type=ch.type, ok=ok, detail=detail[:500],
                                            alerts=",".join(str(item.id) for _, item in entries)))
        finally:
            if own_client:
                client.close()
    # Attempted means handled until the next repeat, even if a channel failed (failures are logged).
    for a, item in outgoing:
        if item.resolved:
            a.resolve_notified = True
        else:
            a.notified_at = now
        session.add(a)
    session.commit()
    return {"firing": len(current), "notified": len(outgoing), "sent": sent, "failed": failed}


def acknowledge(session: Session, alert_id: int, who: str) -> Alert:
    a = session.get(Alert, alert_id)
    if not a:
        raise LookupError(f"alert {alert_id} not found")
    a.acked_by, a.acked_at = who or "someone", utcnow()
    session.add(a)
    session.commit()
    return a


def silence(session: Session, matcher: str, minutes: int, reason: str, who: str) -> Silence:
    s = Silence(matcher=matcher, until=utcnow() + timedelta(minutes=minutes), reason=reason, created_by=who)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def availability(session: Session, days: int = 7) -> list[dict]:
    """Share of the window each resource spent up, from its status transitions."""
    now = utcnow()
    start = now - timedelta(days=days)
    out = []
    for st in session.exec(select(ResourceState)):
        events = list(session.exec(select(Event).where(Event.address == st.address, Event.ts >= start)
                                   .order_by(Event.ts)))
        events = [e for e in events if e.new_status in ("up", "degraded", "down", "unknown")]
        before = session.exec(select(Event).where(Event.address == st.address, Event.ts < start)
                              .order_by(Event.ts.desc()).limit(1)).first()
        status = before.new_status if before else (events[0].old_status if events else st.status)
        cursor, up, down_incidents = max(start, st.created_at), 0.0, 0
        for e in events:
            if status == "up":
                up += (e.ts - cursor).total_seconds()
            if e.new_status == "down":
                down_incidents += 1
            status, cursor = e.new_status, e.ts
        if status == "up":
            up += (now - cursor).total_seconds()
        span = (now - max(start, st.created_at)).total_seconds()
        out.append({"address": st.address, "env": st.env, "kind": st.kind,
                    "availability": round(up / span, 4) if span > 0 else None, "downIncidents": down_incidents})
    return sorted(out, key=lambda r: (r["availability"] if r["availability"] is not None else 2, r["address"]))
