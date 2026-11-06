"""REST API (/api/v1)."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from labforge import service
from labforge.iac.loader import LoadError, parse_text
from labforge.iac.workspace import WorkspaceError
from labforge.models import AgentReport, CheckResult, Event, ResourceState, Run

router = APIRouter()


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as s:
        yield s


def require_token(request: Request, x_labforge_token: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.api_token
    if token and not hmac.compare_digest(x_labforge_token or "", token):
        raise HTTPException(401, "invalid or missing X-LabForge-Token")


def _resource_json(st: ResourceState) -> dict:
    return {
        "address": st.address, "env": st.env, "tier": st.tier, "kind": st.kind, "name": st.name,
        "status": st.status, "statusDetail": st.status_detail, "checkedAt": st.checked_at,
        "updatedAt": st.updated_at, "spec": st.spec, "outputs": st.outputs,
    }


class PlanRequest(BaseModel):
    path: str | None = None


class ApplyRequest(PlanRequest):
    dry_run: bool = True
    allow_prod: bool = False


class AgentPayload(BaseModel):
    env: str
    name: str
    hostname: str | None = None
    os: str | None = None
    arch: str | None = None
    cpu_percent: float
    mem_percent: float
    disk_percent: float | None = None
    temp_c: float | None = None
    extra: dict = {}


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/resources")
def list_resources(env: str | None = None, kind: str | None = None, status: str | None = None,
                   session: Session = Depends(get_session)) -> list[dict]:
    q = select(ResourceState).order_by(ResourceState.address)
    if env:
        q = q.where(ResourceState.env == env)
    if kind:
        q = q.where(ResourceState.kind == kind)
    if status:
        q = q.where(ResourceState.status == status)
    return [_resource_json(st) for st in session.exec(q)]


def _get_resource(session: Session, address: str) -> ResourceState:
    st = session.exec(select(ResourceState).where(ResourceState.address == address)).first()
    if not st:
        raise HTTPException(404, f"resource {address} not found")
    return st


@router.get("/resources/{address}")
def get_resource(address: str, session: Session = Depends(get_session)) -> dict:
    st = _get_resource(session, address)
    checks = session.exec(select(CheckResult).where(CheckResult.address == address).order_by(CheckResult.probe))
    return {**_resource_json(st), "checks": [c.model_dump() for c in checks]}


@router.get("/resources/{address}/metrics")
def get_metrics(address: str, limit: int = 120, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.exec(select(AgentReport).where(AgentReport.address == address)
                        .order_by(AgentReport.ts.desc()).limit(min(limit, 2000)))
    return [r.model_dump(exclude={"payload_json"}) | {"payload": r.payload} for r in rows]


@router.get("/events")
def list_events(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    return [e.model_dump() for e in session.exec(select(Event).order_by(Event.ts.desc()).limit(min(limit, 1000)))]


def _iac_path(request: Request, path: str | None) -> Path:
    return Path(path) if path else request.app.state.settings.iac_path


@router.post("/iac/plan", dependencies=[Depends(require_token)])
def plan(req: PlanRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    try:
        _, p = service.make_plan(session, _iac_path(request, req.path))
    except LoadError as e:
        raise HTTPException(422, {"errors": e.errors}) from e
    return {
        "envs": p.envs,
        "counts": p.counts(),
        "changes": [{"action": c.action, "address": c.address, "tier": c.tier, "diff": c.diff}
                    for c in p.changes],
    }


@router.post("/iac/apply", dependencies=[Depends(require_token)], status_code=202)
def apply(req: ApplyRequest, request: Request) -> dict:
    path = _iac_path(request, req.path)
    run_id = service.start_run_background(request.app.state.engine, request.app.state.settings, path,
                                          dry_run=req.dry_run, allow_prod=req.allow_prod, source=f"api:{path}")
    return {"runId": run_id}


@router.get("/runs")
def list_runs(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.exec(select(Run).order_by(Run.id.desc()).limit(min(limit, 500)))
    return [r.model_dump(exclude={"log", "summary_json"}) | {"summary": r.summary} for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run.model_dump(exclude={"summary_json"}) | {"summary": run.summary}


@router.post("/agent/report", dependencies=[Depends(require_token)])
def agent_report(payload: AgentPayload, session: Session = Depends(get_session)) -> dict:
    report, managed = service.ingest_agent_report(session, payload.model_dump())
    return {"address": report.address, "managed": managed}


# --- IaC workspace files ----------------------------------------------------


def _ws_call(fn):
    try:
        return fn()
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e


class ValidateRequest(BaseModel):
    text: str
    path: str = "<buffer>"


@router.get("/iac/files")
def list_files(request: Request) -> list[dict]:
    return [{"path": f.path, "size": f.size, "modified": f.modified, "digest": f.digest, "environments": f.envs,
             "resources": f.resources, "problems": [str(d) for d in f.diagnostics]}
            for f in request.app.state.settings.workspace().files()]


@router.get("/iac/files/{path:path}")
def get_file(path: str, request: Request) -> dict:
    text, digest = _ws_call(lambda: request.app.state.settings.workspace().read(path))
    return {"path": path, "digest": digest, "text": text}


@router.put("/iac/files/{path:path}", dependencies=[Depends(require_token)])
def put_file(path: str, request: Request, text: str = Body(..., media_type="text/plain"),
             if_match: str | None = Header(default=None)) -> dict:
    """Create or replace a file. Send the digest you edited from as If-Match to avoid overwriting others."""
    digest = _ws_call(lambda: request.app.state.settings.workspace().write(path, text, if_match))
    _, diags = parse_text(text, path)
    return {"path": path, "digest": digest, "problems": [str(d) for d in diags]}


@router.delete("/iac/files/{path:path}", dependencies=[Depends(require_token)], status_code=204)
def delete_file(path: str, request: Request) -> Response:
    _ws_call(lambda: request.app.state.settings.workspace().delete(path))
    return Response(status_code=204)


@router.post("/iac/validate")
def validate(req: ValidateRequest) -> dict:
    docs, diags = parse_text(req.text, req.path)
    return {"valid": not diags, "environments": [d.metadata.name for d in docs],
            "problems": [{"line": d.line, "message": d.message} for d in diags]}


@router.get("/iac/export")
def export_workspace(request: Request) -> Response:
    return Response(request.app.state.settings.workspace().export_zip(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="labforge-iac.zip"'})


# --- device test rigs ---------------------------------------------------------


class CheckoutRequest(BaseModel):
    pool: str
    holder: str
    ttl_seconds: int = 1800
    purpose: str = ""
    queued_seconds: float | None = None  # how long the caller has been waiting (for queue trends)


class ReleaseRequest(BaseModel):
    result: str
    restore: bool = True


class RenewRequest(BaseModel):
    ttl_seconds: int = 1800


def _rig_call(fn):
    from labforge.rigs import RigError

    try:
        return fn()
    except RigError as e:
        raise HTTPException(e.status, e.detail) from e


@router.get("/rigs")
def rig_pools(session: Session = Depends(get_session)) -> list[dict]:
    from labforge.rigs import lease_view, overview

    return [{"pool": p["pool"], "free": p["free"], "devices": [
        {"device": d["state"].name, "address": d["state"].address, "status": d["state"].status, "rig": d["rig"],
         "consecutiveFailures": d["health"].consecutive_failures, "quarantineReason": d["health"].reason,
         "lease": lease_view(d["lease"], d["state"]) if d["lease"] else None}
        for d in p["devices"]]} for p in overview(session)]


@router.post("/rigs/checkout", dependencies=[Depends(require_token)], status_code=201)
def rig_checkout(req: CheckoutRequest, request: Request) -> dict:
    """Borrow a healthy device from a pool. 409 when none is free and healthy."""
    from labforge import rigs

    return _rig_call(lambda: rigs.checkout(request.app.state.engine, req.pool, req.holder,
                                           ttl_seconds=req.ttl_seconds, purpose=req.purpose,
                                           queued_seconds=req.queued_seconds))


@router.get("/rigs/trends")
def rig_trends(days: int = 7, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.rigs import trends

    return trends(session, min(max(days, 1), 90))


@router.post("/rigs/leases/{lease_id}/release", dependencies=[Depends(require_token)])
def rig_release(lease_id: int, req: ReleaseRequest, request: Request) -> dict:
    from labforge import rigs

    return _rig_call(lambda: rigs.release(request.app.state.engine, lease_id, req.result, restore=req.restore))


@router.post("/rigs/leases/{lease_id}/renew", dependencies=[Depends(require_token)])
def rig_renew(lease_id: int, req: RenewRequest, request: Request) -> dict:
    from labforge import rigs

    return _rig_call(lambda: rigs.renew(request.app.state.engine, lease_id, req.ttl_seconds))


class FlashRequest(BaseModel):
    version: str
    sha256: str | None = None
    size: int | None = None
    wait_seconds: float = 60.0


@router.post("/rigs/leases/{lease_id}/flash", dependencies=[Depends(require_token)])
def rig_flash(lease_id: int, req: FlashRequest, request: Request) -> dict:
    """Install a firmware build on a leased device and wait until it runs healthy."""
    from labforge import rigs

    return _rig_call(lambda: rigs.flash(request.app.state.engine, lease_id, req.version, sha256=req.sha256,
                                        size=req.size, wait_seconds=req.wait_seconds))


@router.get("/rigs/leases/{lease_id}/device")
def rig_device(lease_id: int, request: Request) -> dict:
    """Health and running firmware of a leased device."""
    from labforge import rigs

    return _rig_call(lambda: rigs.device_status(request.app.state.engine, lease_id))


@router.get("/rigs/leases")
def rig_leases(active: bool = False, limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.models import Lease
    from labforge.rigs import lease_view

    q = select(Lease).order_by(Lease.id.desc()).limit(min(limit, 1000))
    if active:
        q = q.where(Lease.status == "active")
    states = {s.address: s for s in session.exec(select(ResourceState))}
    return [lease_view(lease, states.get(lease.address)) for lease in session.exec(q)]


@router.post("/rigs/devices/{address}/unquarantine", dependencies=[Depends(require_token)], status_code=204)
def rig_unquarantine(address: str, request: Request) -> Response:
    from labforge import rigs

    _rig_call(lambda: rigs.unquarantine(request.app.state.engine, address))
    return Response(status_code=204)
