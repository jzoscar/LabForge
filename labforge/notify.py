"""Notification channel adapters.

Each adapter turns a batch of alert changes into the provider's API call. Secrets (webhook URLs,
tokens) come from the SecretResolver; nothing is stored in IaC. An `httpx.Client` is passed in so
tests can capture requests without sending anything.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Channel

ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "resolved": "✅"}
PD_SEVERITY = {"critical": "critical", "warning": "warning", "info": "info"}
OPSGENIE_PRIORITY = {"critical": "P1", "warning": "P3", "info": "P5"}


@dataclass
class Item:
    """One alert in a notification."""

    id: int
    fingerprint: str
    subject: str
    severity: str
    summary: str
    env: str = ""
    tier: str = ""
    kind: str = ""
    resolved: bool = False
    repeat: bool = False

    @property
    def line(self) -> str:
        state = "resolved" if self.resolved else self.severity
        tag = "RESOLVED" if self.resolved else (self.severity.upper() + (" (still firing)" if self.repeat else ""))
        return f"{ICON[state]} {tag} {self.subject}: {self.summary}"


@dataclass
class Notification:
    items: list[Item]
    base_url: str
    title: str = field(init=False)

    def __post_init__(self) -> None:
        firing = sum(1 for i in self.items if not i.resolved)
        resolved = len(self.items) - firing
        parts = [f"{firing} firing" if firing else "", f"{resolved} resolved" if resolved else ""]
        self.title = "[LabForge] " + ", ".join(p for p in parts if p)

    @property
    def text(self) -> str:
        return "\n".join([self.title, *(i.line for i in self.items), f"{self.base_url}/alerts"])


class ChannelError(Exception):
    pass


def send(channel: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    sender = {
        "slack": _slack, "teams": _teams, "webhook": _webhook, "whatsapp": _whatsapp, "sms": _sms,
        "pagerduty": _pagerduty, "opsgenie": _opsgenie, "email": _email,
    }[channel.type]
    try:
        sender(channel, note, secrets, client)
    except httpx.HTTPStatusError as e:
        raise ChannelError(f"{channel.type}: HTTP {e.response.status_code} {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise ChannelError(f"{channel.type}: {type(e).__name__}: {e}") from e


def _secret(secrets: SecretResolver, ref: str | None, what: str) -> str:
    value = secrets.get_optional(ref)
    if not value:
        raise ChannelError(f"secret {ref!r} for {what} is not set")
    return value


def _post(client: httpx.Client, url: str, **kw) -> httpx.Response:
    r = client.post(url, timeout=15, **kw)
    r.raise_for_status()
    return r


# --- chat ---------------------------------------------------------------------


def slack_blocks(note: Notification) -> list[dict]:
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": note.title}}]
    for item in note.items:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": item.line}})
        if not item.resolved:
            buttons = [
                {"type": "button", "text": {"type": "plain_text", "text": "Acknowledge"}, "style": "primary",
                 "action_id": "ack", "value": str(item.id)},
                {"type": "button", "text": {"type": "plain_text", "text": "Silence 1h"},
                 "action_id": "silence_1h", "value": str(item.id)},
            ]
            if item.kind == "host" and item.fingerprint.endswith(":quarantine"):
                buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Back to pool"},
                                "action_id": "unquarantine", "value": str(item.id)})
            blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{note.base_url}/alerts|Open LabForge>"}]})
    return blocks


def _slack(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    _post(client, _secret(secrets, ch.url_ref, ch.name), json={"text": note.text, "blocks": slack_blocks(note)})


def _teams(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    body = [{"type": "TextBlock", "text": note.title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    body += [{"type": "TextBlock", "text": i.line, "wrap": True} for i in note.items]
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
            "body": body, "actions": [{"type": "Action.OpenUrl", "title": "Open LabForge", "url": f"{note.base_url}/alerts"}]}
    _post(client, _secret(secrets, ch.url_ref, ch.name),
          json={"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                     "content": card}]})


def _webhook(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    url = ch.url or _secret(secrets, ch.url_ref, ch.name)
    _post(client, url, json={"title": note.title, "text": note.text, "alerts": [i.__dict__ for i in note.items]})


# --- phones -------------------------------------------------------------------


def _twilio(ch: Channel, body: str, secrets: SecretResolver, client: httpx.Client, prefix: str) -> None:
    token = _secret(secrets, ch.token_ref, ch.name)
    url = f"https://api.twilio.com/2010-04-01/Accounts/{ch.account_sid}/Messages.json"
    for number in ch.to:
        _post(client, url, auth=(ch.account_sid or "", token),
              data={"From": f"{prefix}{ch.sender}", "To": f"{prefix}{number}", "Body": body})


def _whatsapp(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    if ch.provider == "twilio":
        _twilio(ch, note.text, secrets, client, "whatsapp:")
        return
    token = _secret(secrets, ch.token_ref, ch.name)
    url = f"https://graph.facebook.com/v20.0/{ch.phone_number_id}/messages"
    for number in ch.to:
        if ch.template:  # outside the 24h service window WhatsApp only accepts approved templates
            message = {"type": "template", "template": {
                "name": ch.template, "language": {"code": "en"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": note.text[:1000]}]}]}}
        else:
            message = {"type": "text", "text": {"body": note.text[:4096]}}
        _post(client, url, headers={"Authorization": f"Bearer {token}"},
              json={"messaging_product": "whatsapp", "to": number.lstrip("+"), **message})


def _sms(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    lines = [note.title, *(i.line for i in note.items[:5])]
    if len(note.items) > 5:
        lines.append(f"+{len(note.items) - 5} more")
    _twilio(ch, "\n".join(lines)[:1500], secrets, client, "")


# --- on-call ------------------------------------------------------------------


def _pagerduty(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    key = _secret(secrets, ch.routing_key_ref, ch.name)
    for i in note.items:  # one event per alert so PagerDuty can dedupe and resolve it
        event = {"routing_key": key, "dedup_key": i.fingerprint,
                 "event_action": "resolve" if i.resolved else "trigger",
                 "links": [{"href": f"{note.base_url}/alerts", "text": "LabForge"}]}
        if not i.resolved:
            event["payload"] = {"summary": f"{i.subject}: {i.summary}"[:1024], "source": i.subject,
                                "severity": PD_SEVERITY.get(i.severity, "warning"), "component": i.kind,
                                "group": i.env, "class": i.tier}
        _post(client, "https://events.pagerduty.com/v2/enqueue", json=event)


def _opsgenie(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    headers = {"Authorization": f"GenieKey {_secret(secrets, ch.api_key_ref, ch.name)}"}
    for i in note.items:
        if i.resolved:
            _post(client, f"https://api.opsgenie.com/v2/alerts/{i.fingerprint}/close",
                  params={"identifierType": "alias"}, headers=headers, json={"source": "labforge"})
        else:
            _post(client, "https://api.opsgenie.com/v2/alerts", headers=headers, json={
                "message": f"{i.subject}: {i.summary}"[:130], "alias": i.fingerprint, "description": i.summary,
                "priority": OPSGENIE_PRIORITY.get(i.severity, "P3"), "tags": [i.env, i.tier, i.kind], "source": "labforge"})


def _email(ch: Channel, note: Notification, secrets: SecretResolver, client: httpx.Client) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = note.title, ch.sender, ", ".join(ch.to)
    msg.set_content(note.text)
    try:
        with smtplib.SMTP(ch.smtp_host or "", ch.smtp_port, timeout=15) as smtp:
            if ch.starttls:
                smtp.starttls()
            if ch.smtp_user_ref:
                smtp.login(*secrets.credentials(ch.smtp_user_ref))
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise ChannelError(f"email: {e}") from e
