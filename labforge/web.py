"""Server-rendered web UI."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlmodel import Session, select

from labforge.api import get_session
from labforge.models import AgentReport, CheckResult, Event, ResourceState, Run, utcnow

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_LABEL = {"up": "Up", "degraded": "Degraded", "down": "Down", "unknown": "Unknown"}
KIND_LABEL = {"network": "Network/VPC", "loadbalancer": "Load balancer", "host": "Host", "container": "Container"}
TIER_LABEL = {"dev": "Dev", "test": "Test", "rnd": "R&D", "staging": "Staging", "demo": "Demo", "prod": "Prod"}


def ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    s = int((utcnow() - dt).total_seconds())
    if s < 60:
        return f"{max(s, 0)}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


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


def until(dt: datetime | None) -> str:
    if not dt:
        return "—"
    s = int((dt - utcnow()).total_seconds())
    if s <= 0:
        return "now"
    return f"in {s // 60}m" if s < 3600 else f"in {s // 3600}h {s % 3600 // 60}m"


templates.env.filters["ago"] = ago
templates.env.filters["until"] = until
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


SEVERITY = {"down": 0, "degraded": 1, "unknown": 2}


def dashboard_context(session: Session, agent_stale_after: int) -> dict:
    """The overview answers three questions: is the lab healthy, what needs attention,
    and how is the physical test hardware doing. Everything else lives on /resources."""
    states = list(session.exec(select(ResourceState).order_by(ResourceState.address)))
    hosts = [s for s in states if s.kind == "host"]
    reports = _latest_reports(session, [h.address for h in hosts])
    checks = _checks(session)
    now = utcnow()

    envs: dict[str, dict] = {}
    for s in states:
        e = envs.setdefault(s.env, {"tier": s.tier, "kinds": Counter(), "status": Counter()})
        e["kinds"][s.kind] += 1
        e["status"][s.status] += 1

    expect_agent = [h for h in hosts if h.spec.get("monitor", {}).get("agent")]
    agents_live = sum(1 for h in expect_agent
                      if h.address in reports and (now - reports[h.address].ts).total_seconds() <= agent_stale_after)
    hardware = [h for h in hosts if h.spec.get("kind") == "baremetal"]
    checked = [s.checked_at for s in states if s.checked_at]

    return {
        "totals": Counter(s.status for s in states),
        "total": len(states),
        "attention": sorted((s for s in states if s.status != "up"),
                            key=lambda s: (SEVERITY.get(s.status, 3), s.address)),
        "agents": {"live": agents_live, "expected": len(expect_agent)},
        "devices": {
            "total": len(hardware),
            "arm": sum(1 for h in hardware if h.spec["platform"]["arch"] in ("arm64", "armv7")),
            "windows": sum(1 for h in hardware if h.spec["platform"]["os"] == "windows"),
        },
        "last_sweep": max(checked) if checked else None,
        "envs": dict(sorted(envs.items())),
        "hardware": [{"st": h, "spec": h.spec, "report": reports.get(h.address), "bmc": checks[h.address].get("bmc")}
                     for h in hardware],
        "other_count": len(states) - len(hardware),
        "events": list(session.exec(select(Event).order_by(Event.ts.desc()).limit(8))),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    ctx = dashboard_context(session, request.app.state.settings.agent_stale_after)
    return templates.TemplateResponse(request, "dashboard.html", {"nav": "dash", **ctx})


@router.get("/partials/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request, session: Session = Depends(get_session)):
    ctx = dashboard_context(session, request.app.state.settings.agent_stale_after)
    return templates.TemplateResponse(request, "_dashboard_body.html", ctx)


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
            "Memory %": sparkline([r.mem_percent for r in reports]),
            "Disk %": sparkline([r.disk_percent for r in reports]),
            "Temp °C": sparkline([r.temp_c for r in reports], vmax=110),
        }
    return templates.TemplateResponse(request, "resource.html", {
        "nav": "res", "st": st, "checks": checks, "events": events,
        "latest": reports[-1] if reports else None, "spark": spark,
        "spec_yaml": yaml.safe_dump(st.spec, sort_keys=False, allow_unicode=True),
        "outputs_yaml": yaml.safe_dump(st.outputs, sort_keys=False, allow_unicode=True),
    })


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
