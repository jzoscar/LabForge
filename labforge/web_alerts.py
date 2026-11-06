"""Alerts in the web UI: firing alerts, silences, channels, notification log, availability."""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from labforge import alerting
from labforge.api import get_session
from labforge.iac.loader import load_policies
from labforge.iac.secrets import SecretResolver
from labforge.models import Alert, NotificationLog, Silence, utcnow
from labforge.notify import ChannelError, Item, Notification, send
from labforge.auth import Principal, audit, web_require
from labforge.web import templates

router = APIRouter(prefix="/alerts", include_in_schema=False)


def _require_edit(request: Request) -> Principal:
    if not request.app.state.settings.ui_edit:
        raise HTTPException(403, "changing alerts from the web UI is disabled (LABFORGE_UI_EDIT=0)")
    return web_require(request, "operator")


def _back(message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in {"message": message, "error": error}.items() if v})
    return RedirectResponse(f"/alerts{'?' + query if query else ''}", status_code=303)


@router.get("", response_class=HTMLResponse)
def alerts_page(request: Request, session: Session = Depends(get_session), message: str = "", error: str = ""):
    settings = request.app.state.settings
    policies = load_policies(settings.workspace().root)
    secrets = SecretResolver()
    channels = [(p.metadata.name, c, bool(_configured(c, secrets))) for p in policies for c in p.spec.channels]
    firing = list(session.exec(select(Alert).where(Alert.status == "firing")))
    firing.sort(key=lambda a: (-alerting.RANK.get(a.severity, 0), a.started_at))
    return templates.TemplateResponse(request, "alerts.html", {
        "nav": "alerts", "firing": firing, "ui_edit": settings.ui_edit, "message": message, "error": error,
        "resolved": list(session.exec(select(Alert).where(Alert.status == "resolved")
                                      .order_by(Alert.resolved_at.desc()).limit(15))),
        "silences": list(session.exec(select(Silence).where(Silence.until > utcnow()).order_by(Silence.until))),
        "channels": channels, "policies": policies,
        "log": list(session.exec(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(20))),
        "availability": alerting.availability(session, 7)[:12],
    })


def _configured(channel, secrets: SecretResolver) -> bool:
    refs = [channel.url_ref, channel.token_ref, channel.routing_key_ref, channel.api_key_ref]
    return all(secrets.get_optional(r) for r in refs if r) and (channel.type != "webhook" or channel.url or channel.url_ref)


@router.post("/ack")
def ack(request: Request, alert_id: int = Form(...), who: str = Form("web")):
    principal = _require_edit(request)
    with _session(request) as session:
        try:
            a = alerting.acknowledge(session, alert_id, principal.name if principal.via == "sso" else (who or "web"))
        except LookupError as e:
            return _back(error=str(e))
        audit(request.app.state.engine, principal, "alert.ack", a.subject)
        return _back(message=f"{a.subject} acknowledged")


@router.post("/silence")
def silence(request: Request, matcher: str = Form(...), minutes: int = Form(60), reason: str = Form("")):
    principal = _require_edit(request)
    with _session(request) as session:
        s = alerting.silence(session, matcher.strip(), minutes, reason, principal.name)
        audit(request.app.state.engine, principal, "alert.silence", s.matcher, f"{minutes} min: {reason}")
        return _back(message=f"Silenced {s.matcher} for {minutes} min")


@router.post("/unsilence")
def unsilence(request: Request, silence_id: int = Form(...)):
    _require_edit(request)
    with _session(request) as session:
        row = session.get(Silence, silence_id)
        if row:
            session.delete(row)
            session.commit()
        return _back(message="Silence removed")


@router.post("/test")
def test_channel(request: Request, channel: str = Form(...)):
    """Send a test notification to one channel so its configuration can be checked."""
    _require_edit(request)
    settings = request.app.state.settings
    found = next((c for p in load_policies(settings.workspace().root) for c in p.spec.channels if c.name == channel), None)
    if not found:
        return _back(error=f"unknown channel {channel}")
    note = Notification([Item(0, "labforge:test", "labforge", "info", "test notification from the Alerts page")],
                        settings.public_url)
    with _session(request) as session, httpx.Client() as client:
        try:
            send(found, note, SecretResolver(), client)
            ok, detail = True, "test sent"
        except ChannelError as e:
            ok, detail = False, str(e)
        session.add(NotificationLog(channel=channel, channel_type=found.type, ok=ok, detail=detail))
        session.commit()
    return _back(message=f"Test sent to {channel}") if ok else _back(error=f"{channel}: {detail}")


def _session(request: Request) -> Session:
    return Session(request.app.state.engine)
