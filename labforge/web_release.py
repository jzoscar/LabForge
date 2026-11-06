"""Release guardrails in the web UI: gate, staged rollouts, production change requests."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from labforge import release, service
from labforge.api import get_session
from labforge.auth import Principal, audit, web_require
from labforge.iac.loader import LoadError
from labforge.models import ChangeRequest, Rollout
from labforge.web import templates

router = APIRouter(prefix="/releases", include_in_schema=False)


def _back(message: str = "", error: str = "", **extra) -> RedirectResponse:
    query = urlencode({k: v for k, v in {"message": message, "error": error, **extra}.items() if v})
    return RedirectResponse(f"/releases{'?' + query if query else ''}", status_code=303)


def _actor(who: Principal, name: str) -> str:
    # Without SSO everyone is "anonymous"; the form asks for a name so approvals stay meaningful.
    return who.name if who.via == "sso" else (name.strip() or who.name)


@router.get("", response_class=HTMLResponse)
def releases_page(request: Request, version: str = "", message: str = "", error: str = "",
                  session: Session = Depends(get_session)):
    pol = release.policy(request.app.state.settings.workspace().root)
    rollouts = [(r, json.loads(r.batches_json))
                for r in session.exec(select(Rollout).order_by(Rollout.id.desc()).limit(10))]
    changes = [(c, json.loads(c.approvals_json), json.loads(c.summary_json))
               for c in session.exec(select(ChangeRequest).order_by(ChangeRequest.id.desc()).limit(20))]
    return templates.TemplateResponse(request, "releases.html", {
        "nav": "releases", "policy": pol, "version": version, "message": message, "error": error,
        "gate": release.gate(session, version, pol) if version else None,
        "rollouts": rollouts, "changes": changes, "ui_edit": request.app.state.settings.ui_edit,
    })


@router.post("/rollout")
def rollout(request: Request, version: str = Form(...), env: str = Form(""), selector: str = Form(""),
            canary: int = Form(1), batch: int = Form(2), bake: int = Form(60), force: bool = Form(False)):
    who = web_require(request, "admin")
    sel = dict(part.split("=", 1) for part in selector.split(",") if "=" in part) if selector.strip() else None
    pol = release.policy(request.app.state.settings.workspace().root)
    try:
        rid = release.start_rollout(request.app.state.engine, pol, version.strip(), env=env.strip() or None,
                                    selector=sel, canary=canary, batch_size=batch, bake_seconds=bake,
                                    by=who.name, force=force)
    except release.ReleaseError as e:
        failed = [c["check"] for c in e.detail.get("gate", {}).get("checks", []) if not c["ok"]]
        return _back(error=e.detail["error"] + (f": {', '.join(failed)}" if failed else ""), version=version)
    audit(request.app.state.engine, who, "release.rollout", version, f"rollout {rid}" + (" FORCED" if force else ""))
    return _back(message=f"Rollout #{rid} of {version} started", version=version)


@router.post("/abort")
def abort(request: Request, rollout_id: int = Form(...)):
    who = web_require(request, "admin")
    try:
        release.abort(request.app.state.engine, rollout_id)
    except release.ReleaseError as e:
        return _back(error=e.detail["error"])
    audit(request.app.state.engine, who, "release.abort", str(rollout_id))
    return _back(message=f"Rollout #{rollout_id}: aborting, rolling back")


@router.post("/change")
def change(request: Request, path: str = Form(""), reason: str = Form(...), name: str = Form("")):
    who = web_require(request, "operator")
    ws = request.app.state.settings.workspace()
    with Session(request.app.state.engine) as session:
        try:
            _, plan = service.make_plan(session, ws.root / path if path else ws.root)
        except LoadError as e:
            return _back(error=f"IaC problems: {e.errors[0]}")
    try:
        cr = release.request_change(request.app.state.engine, plan, path, reason, _actor(who, name))
    except release.ReleaseError as e:
        return _back(error=e.detail["error"])
    audit(request.app.state.engine, who, "change.request", path or "workspace", f"#{cr.id}: {reason}")
    return _back(message=f"Change request #{cr.id} created; it needs approval by someone else")


@router.post("/decide")
def decide(request: Request, change_id: int = Form(...), decision: str = Form(...), name: str = Form("")):
    who = web_require(request, "admin")
    pol = release.policy(request.app.state.settings.workspace().root)
    try:
        cr = release.decide(request.app.state.engine, change_id, _actor(who, name), decision == "approve", pol)
    except release.ReleaseError as e:
        return _back(error=e.detail["error"])
    audit(request.app.state.engine, who, f"change.{decision}", f"#{change_id}", cr.status)
    return _back(message=f"Change request #{change_id}: {cr.status}")
