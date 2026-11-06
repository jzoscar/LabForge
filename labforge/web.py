"""Server-rendered web UI."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlmodel import Session, select

from labforge import service
from labforge.api import get_session
from labforge.iac.loader import LoadError
from labforge.models import AgentReport, CheckResult, Event, ResourceState, Run, utcnow

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_LABEL = {"up": "正常", "degraded": "降级", "down": "故障", "unknown": "未知"}
KIND_LABEL = {"network": "网络/VPC", "loadbalancer": "负载均衡", "host": "主机", "container": "容器"}
TIER_LABEL = {"dev": "开发", "test": "测试", "rnd": "研发", "staging": "预发/非生产", "prod": "生产"}


def ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    s = int((utcnow() - dt).total_seconds())
    if s < 60:
        return f"{max(s, 0)} 秒前"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


def sparkline(values: list[float | None], width: int = 140, height: int = 30, vmax: float = 100) -> Markup:
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return Markup('<span class="muted">—</span>')
    step = width / (len(pts) - 1)
    coords = " ".join(f"{i * step:.1f},{height - min(v, vmax) / vmax * (height - 2) - 1:.1f}" for i, v in enumerate(pts))
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<polyline points="{coords}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'
    )


templates.env.filters["ago"] = ago
templates.env.globals.update(STATUS_LABEL=STATUS_LABEL, KIND_LABEL=KIND_LABEL, TIER_LABEL=TIER_LABEL)


def _latest_reports(session: Session, addresses: list[str]) -> dict[str, AgentReport]:
    out = {}
    for a in addresses:
        r = session.exec(select(AgentReport).where(AgentReport.address == a).order_by(AgentReport.ts.desc()).limit(1)).first()
        if r:
            out[a] = r
    return out


def _checks(session: Session) -> dict[str, dict[str, CheckResult]]:
    out: dict[str, dict[str, CheckResult]] = defaultdict(dict)
    for c in session.exec(select(CheckResult)):
        out[c.address][c.probe] = c
    return out


def dashboard_context(session: Session) -> dict:
    states = list(session.exec(select(ResourceState).order_by(ResourceState.address)))
    hosts = [s for s in states if s.kind == "host"]
    reports = _latest_reports(session, [h.address for h in hosts])
    checks = _checks(session)

    envs: dict[str, dict] = {}
    for s in states:
        e = envs.setdefault(s.env, {"tier": s.tier, "kinds": Counter(), "status": Counter()})
        e["kinds"][s.kind] += 1
        e["status"][s.status] += 1

    def row(h: ResourceState) -> dict:
        spec = h.spec
        return {"st": h, "spec": spec, "report": reports.get(h.address), "bmc": checks[h.address].get("bmc")}

    return {
        "totals": Counter(s.status for s in states),
        "total": len(states),
        "envs": dict(sorted(envs.items())),
        "hardware": [row(h) for h in hosts if h.spec.get("kind") == "baremetal"],
        "servers": [row(h) for h in hosts if h.spec.get("kind") != "baremetal"],
        "arch": Counter(h.spec["platform"]["arch"] for h in hosts),
        "os": Counter(h.spec["platform"]["os"] for h in hosts),
        "netlb": [s for s in states if s.kind in ("network", "loadbalancer")],
        "containers": [s for s in states if s.kind == "container"],
        "events": list(session.exec(select(Event).order_by(Event.ts.desc()).limit(12))),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "dashboard.html", {"nav": "dash", **dashboard_context(session)})


@router.get("/partials/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "_dashboard_body.html", dashboard_context(session))


@router.get("/resources", response_class=HTMLResponse)
def resources(request: Request, env: str = "", kind: str = "", status: str = "", q: str = "",
              session: Session = Depends(get_session)):
    query = select(ResourceState).order_by(ResourceState.address)
    if env:
        query = query.where(ResourceState.env == env)
    if kind:
        query = query.where(ResourceState.kind == kind)
    if status:
        query = query.where(ResourceState.status == status)
    rows = [r for r in session.exec(query) if q.lower() in r.address.lower()]
    all_envs = sorted({e for e in session.exec(select(ResourceState.env))})
    return templates.TemplateResponse(request, "resources.html", {
        "nav": "res", "rows": rows, "envs": all_envs,
        "f": {"env": env, "kind": kind, "status": status, "q": q},
    })


@router.get("/resources/{address}", response_class=HTMLResponse)
def resource_detail(address: str, request: Request, session: Session = Depends(get_session)):
    st = session.exec(select(ResourceState).where(ResourceState.address == address)).first()
    if not st:
        raise HTTPException(404, "resource not found")
    checks = list(session.exec(select(CheckResult).where(CheckResult.address == address).order_by(CheckResult.probe)))
    reports = list(session.exec(select(AgentReport).where(AgentReport.address == address)
                                .order_by(AgentReport.ts.desc()).limit(120)))[::-1]
    events = list(session.exec(select(Event).where(Event.address == address).order_by(Event.ts.desc()).limit(20)))
    spark = {}
    if reports:
        spark = {
            "CPU %": sparkline([r.cpu_percent for r in reports]),
            "内存 %": sparkline([r.mem_percent for r in reports]),
            "磁盘 %": sparkline([r.disk_percent for r in reports]),
            "温度 °C": sparkline([r.temp_c for r in reports], vmax=110),
        }
    return templates.TemplateResponse(request, "resource.html", {
        "nav": "res", "st": st, "checks": checks, "events": events,
        "latest": reports[-1] if reports else None, "spark": spark,
        "spec_yaml": yaml.safe_dump(st.spec, sort_keys=False, allow_unicode=True),
        "outputs_yaml": yaml.safe_dump(st.outputs, sort_keys=False, allow_unicode=True),
    })


@router.get("/iac", response_class=HTMLResponse)
def iac(request: Request, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    ctx: dict = {"nav": "iac", "path": settings.iac_path, "ui_apply": settings.ui_apply, "errors": None, "plan": None}
    try:
        _, ctx["plan"] = service.make_plan(session, settings.iac_path)
    except LoadError as e:
        ctx["errors"] = e.errors
    return templates.TemplateResponse(request, "iac.html", ctx)


@router.post("/iac/apply")
def iac_apply(request: Request, dry_run: bool = Form(False), allow_prod: bool = Form(False)):
    settings = request.app.state.settings
    if not dry_run and not settings.ui_apply:
        raise HTTPException(403, "real apply from the UI is disabled; set LABFORGE_UI_APPLY=1 or use the CLI")
    run_id = service.start_run_background(request.app.state.engine, settings, settings.iac_path,
                                          dry_run=dry_run, allow_prod=allow_prod, source="web")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, session: Session = Depends(get_session)):
    rows = list(session.exec(select(Run).order_by(Run.id.desc()).limit(100)))
    return templates.TemplateResponse(request, "runs.html", {"nav": "runs", "rows": rows})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return templates.TemplateResponse(request, "run.html", {"nav": "runs", "run": run})
