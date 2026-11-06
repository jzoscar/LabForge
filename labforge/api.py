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
from labforge.auth import Principal, audit, require_envs, requires
from labforge.iac.loader import LoadError, parse_text
from labforge.iac.workspace import WorkspaceError
from labforge.models import AgentReport, CheckResult, Event, ResourceState, Run

router = APIRouter()


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as s:
        yield s




def _resource_json(st: ResourceState) -> dict:
    return {
        "address": st.address, "env": st.env, "tier": st.tier, "kind": st.kind, "name": st.name,
        "status": st.status, "statusDetail": st.status_detail, "checkedAt": st.checked_at,
        "updatedAt": st.updated_at, "spec": st.spec, "outputs": st.outputs,
    }


class PlanRequest(BaseModel):
    path: str | None = None
    # GitOps: plan these files (path -> text, e.g. a pull request's IaC) against this server's state
    files: dict[str, str] | None = None


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


def _env_names(path: Path) -> list[str]:
    from labforge.iac.loader import load_path

    try:
        return [d.metadata.name for d in load_path(path)]
    except LoadError:
        return []  # the apply itself reports the problems


def _iac_path(request: Request, path: str | None) -> Path:
    return Path(path) if path else request.app.state.settings.iac_path


@router.post("/iac/plan", dependencies=[Depends(requires("viewer"))])
def plan(req: PlanRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    import tempfile

    from labforge.iac.planner import plan_markdown
    from labforge.iac.workspace import Workspace, WorkspaceError

    try:
        if req.files is not None:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Workspace(Path(tmp), Path(tmp) / ".history")
                for rel, text in req.files.items():
                    ws.write(rel, text)
                _, p = service.make_plan(session, ws.root)
        else:
            _, p = service.make_plan(session, _iac_path(request, req.path))
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    except LoadError as e:
        raise HTTPException(422, {"errors": e.errors}) from e
    return {
        "markdown": plan_markdown(p),
        "envs": p.envs,
        "counts": p.counts(),
        "changes": [{"action": c.action, "address": c.address, "tier": c.tier, "diff": c.diff}
                    for c in p.changes],
    }


@router.post("/iac/apply", status_code=202)
def apply(req: ApplyRequest, request: Request, who: Principal = Depends(requires("operator"))) -> dict:
    path = _iac_path(request, req.path)
    if not req.dry_run and not who.can("admin"):
        raise HTTPException(403, f"{who.name} ({who.role}) may only dry-run; a real apply needs admin")
    require_envs(who, _env_names(path))
    audit(request.app.state.engine, who, "apply.start", str(path), f"dry_run={req.dry_run}", "api")
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


@router.post("/agent/report")
def agent_report(payload: AgentPayload, session: Session = Depends(get_session),
                 who: Principal = Depends(requires("agent"))) -> dict:
    if not who.may_report_as(f"{payload.env}.host.{payload.name}"):
        raise HTTPException(403, f"{who.name} may only report as {who.subject}")
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


@router.put("/iac/files/{path:path}", dependencies=[Depends(requires("editor"))])
def put_file(path: str, request: Request, text: str = Body(..., media_type="text/plain"),
             if_match: str | None = Header(default=None)) -> dict:
    """Create or replace a file. Send the digest you edited from as If-Match to avoid overwriting others."""
    digest = _ws_call(lambda: request.app.state.settings.workspace().write(path, text, if_match))
    audit(request.app.state.engine, request.state.principal, "iac.save", path, "", "api")
    _, diags = parse_text(text, path)
    return {"path": path, "digest": digest, "problems": [str(d) for d in diags]}


@router.delete("/iac/files/{path:path}", dependencies=[Depends(requires("editor"))], status_code=204)
def delete_file(path: str, request: Request) -> Response:
    _ws_call(lambda: request.app.state.settings.workspace().delete(path))
    audit(request.app.state.engine, request.state.principal, "iac.delete", path, "", "api")
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


@router.post("/rigs/checkout", dependencies=[Depends(requires("operator"))], status_code=201)
def rig_checkout(req: CheckoutRequest, request: Request) -> dict:
    """Borrow a healthy device from a pool. 409 when none is free and healthy."""
    from labforge import rigs

    lease = _rig_call(lambda: rigs.checkout(request.app.state.engine, req.pool, req.holder,
                                            ttl_seconds=req.ttl_seconds, purpose=req.purpose,
                                            queued_seconds=req.queued_seconds))
    audit(request.app.state.engine, request.state.principal, "rig.checkout", lease["address"],
          f"lease {lease['leaseId']} to {req.holder}", "api")
    return lease


@router.get("/rigs/trends")
def rig_trends(days: int = 7, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.rigs import trends

    return trends(session, min(max(days, 1), 90))


@router.post("/rigs/leases/{lease_id}/release", dependencies=[Depends(requires("operator"))])
def rig_release(lease_id: int, req: ReleaseRequest, request: Request) -> dict:
    from labforge import rigs

    lease = _rig_call(lambda: rigs.release(request.app.state.engine, lease_id, req.result, restore=req.restore))
    audit(request.app.state.engine, request.state.principal, "rig.release", lease["address"],
          f"lease {lease_id}: {req.result}", "api")
    return lease


@router.post("/rigs/leases/{lease_id}/renew", dependencies=[Depends(requires("operator"))])
def rig_renew(lease_id: int, req: RenewRequest, request: Request) -> dict:
    from labforge import rigs

    return _rig_call(lambda: rigs.renew(request.app.state.engine, lease_id, req.ttl_seconds))


class FlashRequest(BaseModel):
    version: str
    sha256: str | None = None
    size: int | None = None
    wait_seconds: float = 60.0


@router.post("/rigs/leases/{lease_id}/flash", dependencies=[Depends(requires("operator"))])
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


@router.post("/rigs/devices/{address}/unquarantine", dependencies=[Depends(requires("operator"))], status_code=204)
def rig_unquarantine(address: str, request: Request) -> Response:
    from labforge import rigs

    _rig_call(lambda: rigs.unquarantine(request.app.state.engine, address))
    audit(request.app.state.engine, request.state.principal, "rig.unquarantine", address, "", "api")
    return Response(status_code=204)


# --- alerts ---------------------------------------------------------------------


class SilenceRequest(BaseModel):
    matcher: str  # glob on the alert subject, e.g. "staging.*" or "pool:sim-sensor"
    minutes: int = 60
    reason: str = ""
    created_by: str = ""


@router.get("/alerts")
def list_alerts(status: str | None = "firing", limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.models import Alert

    q = select(Alert).order_by(Alert.started_at.desc()).limit(min(limit, 1000))
    if status:
        q = q.where(Alert.status == status)
    return [a.model_dump() for a in session.exec(q)]


@router.post("/alerts/{alert_id}/ack", dependencies=[Depends(requires("operator"))])
def ack_alert(alert_id: int, who: str = "", session: Session = Depends(get_session)) -> dict:
    from labforge.alerting import acknowledge

    try:
        return acknowledge(session, alert_id, who or "api").model_dump()
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/silences", dependencies=[Depends(requires("operator"))], status_code=201)
def create_silence(req: SilenceRequest, session: Session = Depends(get_session)) -> dict:
    from labforge.alerting import silence

    return silence(session, req.matcher, req.minutes, req.reason, req.created_by or "api").model_dump()


@router.delete("/silences/{silence_id}", dependencies=[Depends(requires("operator"))], status_code=204)
def delete_silence(silence_id: int, session: Session = Depends(get_session)) -> Response:
    from labforge.models import Silence

    row = session.get(Silence, silence_id)
    if row:
        session.delete(row)
        session.commit()
    return Response(status_code=204)


@router.get("/reports/availability")
def availability_report(days: int = 7, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.alerting import availability

    return availability(session, min(max(days, 1), 90))


@router.post("/alerts/slack/actions", include_in_schema=False)
async def slack_actions(request: Request, session: Session = Depends(get_session)) -> dict:
    """Slack interactivity endpoint: Acknowledge / Silence 1h / Back to pool buttons on alerts.
    Requests are verified with the Slack app's signing secret (LABFORGE_SLACK_SIGNING_SECRET)."""
    import json
    import time as _time
    from urllib.parse import parse_qs

    from labforge.alerting import acknowledge, silence
    from labforge.models import Alert

    secret = request.app.state.settings.slack_signing_secret
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "0")
    expected = "v0=" + hmac.new((secret or "").encode(), f"v0:{ts}:".encode() + body, "sha256").hexdigest()
    if not secret or not ts.isdigit() or abs(_time.time() - int(ts)) > 300 \
            or not hmac.compare_digest(expected, request.headers.get("X-Slack-Signature", "")):
        raise HTTPException(401, "invalid Slack signature")

    payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
    action = (payload.get("actions") or [{}])[0]
    user = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id", "slack")
    alert = session.get(Alert, int(action.get("value") or 0))
    if not alert:
        return {"text": "That alert no longer exists."}
    if action.get("action_id") == "ack":
        acknowledge(session, alert.id, f"slack:{user}")
        return {"text": f"{alert.subject} acknowledged by {user}"}
    if action.get("action_id") == "silence_1h":
        silence(session, alert.subject, 60, f"silenced from Slack by {user}", f"slack:{user}")
        return {"text": f"{alert.subject} silenced for 1 hour by {user}"}
    if action.get("action_id") == "unquarantine":
        from labforge import rigs

        try:
            rigs.unquarantine(request.app.state.engine, alert.subject)
        except rigs.RigError as e:
            return {"text": e.detail["error"]}
        return {"text": f"{alert.subject} returned to its pool by {user}"}
    return {"text": "Unknown action"}


# --- builds -------------------------------------------------------------------------


@router.post("/builds", dependencies=[Depends(requires("operator"))], status_code=201)
def ingest_build(report: dict = Body(...), runner: str = "", pipeline: str | None = None,
                 queued_seconds: float | None = None, session: Session = Depends(get_session)) -> dict:
    """Ingest a BuildPulse report.json (the file's JSON as the body)."""
    from labforge.builds import ingest

    if "run_id" not in report or "stages" not in report:
        raise HTTPException(422, "not a BuildPulse report.json (needs run_id and stages)")
    row, created = ingest(session, report, runner=runner, pipeline=pipeline, queued_seconds=queued_seconds)
    return {"id": row.id, "runId": row.run_id, "pipeline": row.pipeline, "created": created}


@router.get("/builds")
def list_builds(pipeline: str | None = None, limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    from labforge.models import BuildRun

    q = select(BuildRun).order_by(BuildRun.id.desc()).limit(min(limit, 1000))
    if pipeline:
        q = q.where(BuildRun.pipeline == pipeline)
    return [r.model_dump(exclude={"stages_json", "cache_json", "meta_json"}) | {"stages": r.stages, "cache": r.cache}
            for r in session.exec(q)]


@router.get("/builds/stats")
def build_stats(days: int = 30, session: Session = Depends(get_session)) -> dict:
    from labforge.builds import stats, suggestions

    pipelines = stats(session, min(max(days, 1), 365))
    return {"pipelines": pipelines, "suggestions": suggestions(pipelines)}


# --- tokens and audit -----------------------------------------------------------------


class TokenRequest(BaseModel):
    name: str
    role: str = "viewer"
    envs: list[str] = ["*"]
    subject: str | None = None


@router.get("/tokens", dependencies=[Depends(requires("admin"))])
def list_tokens(session: Session = Depends(get_session)) -> list[dict]:
    from labforge.models import ApiToken

    return [t.model_dump(exclude={"token_hash"}) for t in session.exec(select(ApiToken).order_by(ApiToken.name))]


@router.post("/tokens", status_code=201)
def create_api_token(req: TokenRequest, request: Request, who: Principal = Depends(requires("admin"))) -> dict:
    from labforge.auth import create_token

    try:
        token = create_token(request.app.state.engine, req.name, req.role, req.envs, req.subject)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(request.app.state.engine, who, "token.create", req.name, f"role={req.role} envs={','.join(req.envs)}", "api")
    return {"name": req.name, "role": req.role, "token": token, "note": "shown once; store it now"}


@router.delete("/tokens/{name}", status_code=204)
def revoke_api_token(name: str, request: Request, who: Principal = Depends(requires("admin"))) -> Response:
    from labforge.auth import revoke_token

    try:
        revoke_token(request.app.state.engine, name)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    audit(request.app.state.engine, who, "token.revoke", name, "", "api")
    return Response(status_code=204)


@router.get("/audit", dependencies=[Depends(requires("viewer"))])
def audit_log(action: str | None = None, actor: str | None = None, limit: int = 200,
              session: Session = Depends(get_session)) -> list[dict]:
    from labforge.models import AuditEvent

    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 2000))
    if action:
        q = q.where(AuditEvent.action.startswith(action))
    if actor:
        q = q.where(AuditEvent.actor == actor)
    return [e.model_dump() for e in session.exec(q)]


# --- CRA: firmware inventory, SBOMs, vulnerability impact ----------------------------------


@router.post("/firmware/{version}/sbom", status_code=201)
def upload_sbom(version: str, request: Request, doc: dict = Body(...), session: Session = Depends(get_session),
                who: Principal = Depends(requires("operator"))) -> dict:
    """Attach the SBOM (CycloneDX or SPDX JSON) of a firmware version."""
    from labforge.cra import store_sbom

    try:
        row = store_sbom(session, version, doc)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    audit(request.app.state.engine, who, "sbom.upload", version, f"{row.format}, {row.component_count} components", "api")
    return {"version": version, "format": row.format, "components": row.component_count}


@router.get("/cra/inventory")
def cra_inventory(session: Session = Depends(get_session)) -> list[dict]:
    from labforge.cra import inventory

    return inventory(session)


@router.get("/cra/affected")
def cra_affected(component: str, below: str | None = None, versions: str | None = None, format: str = "json",
                 session: Session = Depends(get_session)):
    """Which firmware contains a vulnerable component, and which devices run it now (format=markdown for a report)."""
    from labforge.cra import affected, report_markdown

    result = affected(session, component, below=below, versions=versions.split(",") if versions else None)
    if format == "markdown":
        return Response(report_markdown(result), media_type="text/markdown")
    return result
