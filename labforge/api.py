"""REST API (/api/v1)."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from labforge import service
from labforge.iac.loader import LoadError
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
