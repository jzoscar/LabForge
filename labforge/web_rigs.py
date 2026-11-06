"""Device test rigs in the web UI: pools, leases, quarantine."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from labforge import rigs
from labforge.api import get_session
from labforge.models import Lease, ResourceState
from labforge.web import templates

router = APIRouter(prefix="/rigs", include_in_schema=False)


def _require_edit(request: Request) -> None:
    if not request.app.state.settings.ui_edit:
        raise HTTPException(403, "changing rigs from the web UI is disabled (LABFORGE_UI_EDIT=0)")


@router.get("", response_class=HTMLResponse)
def rigs_page(request: Request, session: Session = Depends(get_session), message: str = "", error: str = ""):
    pools = rigs.overview(session)
    devices = [d for p in pools for d in p["devices"]]
    states = {s.address: s for s in session.exec(select(ResourceState))}
    history = [(lease, states.get(lease.address))
               for lease in session.exec(select(Lease).order_by(Lease.id.desc()).limit(25))]
    return templates.TemplateResponse(request, "rigs.html", {
        "nav": "rigs", "pools": pools, "history": history, "ui_edit": request.app.state.settings.ui_edit,
        "trends": {t["pool"]: t for t in rigs.trends(session, 7)},
        "counts": {k: sum(1 for d in devices if d["rig"] == k) for k in ("free", "leased", "quarantined")},
        "total": len(devices), "message": message, "error": error,
    })


def _back(message: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {"message": message, "error": error}.items() if v})
    return RedirectResponse(f"/rigs{'?' + query if query else ''}", status_code=303)


@router.post("/checkout")
def checkout(request: Request, pool: str = Form(...), holder: str = Form(...), ttl_minutes: int = Form(30)):
    _require_edit(request)
    try:
        lease = rigs.checkout(request.app.state.engine, pool, holder, ttl_seconds=ttl_minutes * 60,
                              purpose="manual checkout from the web UI")
    except rigs.RigError as e:
        tried = "; ".join(f"{t['device']}: {t['reason']}" for t in e.detail.get("tried", []))
        return _back(error=e.detail["error"] + (f" (tried {tried})" if tried else ""))
    healed = f" - {lease['note']}" if lease["note"] else ""
    return _back(message=f"Lease #{lease['leaseId']}: {lease['device']} checked out to {lease['holder']}{healed}")


@router.post("/release")
def release(request: Request, lease_id: int = Form(...), result: str = Form("passed")):
    _require_edit(request)
    try:
        lease = rigs.release(request.app.state.engine, lease_id, result)
    except rigs.RigError as e:
        return _back(error=e.detail["error"])
    return _back(message=f"Lease #{lease_id} released ({result}): {lease['note']}")


@router.post("/unquarantine")
def unquarantine(request: Request, address: str = Form(...)):
    _require_edit(request)
    try:
        rigs.unquarantine(request.app.state.engine, address)
    except rigs.RigError as e:
        return _back(error=e.detail["error"])
    return _back(message=f"{address} is back in the pool")
