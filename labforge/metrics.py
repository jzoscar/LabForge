"""Prometheus exposition of LabForge state at /metrics (text format, no client library needed)."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlmodel import Session, select

from labforge.api import get_session
from labforge.models import AgentReport, Alert, CheckResult, Lease, ResourceState

router = APIRouter(include_in_schema=False)
STATUSES = ("up", "degraded", "down", "unknown")


def _labels(**kv) -> str:
    def esc(v) -> str:
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in kv.items()) + "}"


def render(session: Session) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        lines.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}"])
        lines.extend(f"{name}{_labels(**labels)} {value}" for labels, value in samples)

    states = list(session.exec(select(ResourceState)))
    metric("labforge_resource_status", "gauge", "1 for the resource's current status",
           [({"address": s.address, "env": s.env, "tier": s.tier, "kind": s.kind, "status": st}, int(s.status == st))
            for s in states for st in STATUSES])
    by_status = Counter(s.status for s in states)
    metric("labforge_resources", "gauge", "Managed resources by status", [({"status": st}, by_status[st]) for st in STATUSES])

    latest: dict[str, AgentReport] = {}
    for r in session.exec(select(AgentReport).order_by(AgentReport.ts)):
        latest[r.address] = r
    for field, help_ in (("cpu_percent", "CPU %"), ("mem_percent", "Memory %"), ("disk_percent", "Disk %"),
                         ("temp_c", "Temperature (C)")):
        metric(f"labforge_agent_{field}", "gauge", f"{help_} from the latest agent report",
               [({"address": a}, getattr(r, field)) for a, r in latest.items() if getattr(r, field) is not None])

    metric("labforge_probe_ok", "gauge", "1 when the probe passed, 0 when it failed",
           [({"address": c.address, "probe": c.probe}, int(c.ok)) for c in session.exec(select(CheckResult))
            if c.ok is not None])
    metric("labforge_probe_latency_ms", "gauge", "Probe latency",
           [({"address": c.address, "probe": c.probe}, c.latency_ms) for c in session.exec(select(CheckResult))
            if c.latency_ms is not None])

    firing = Counter(a.severity for a in session.exec(select(Alert).where(Alert.status == "firing")))
    metric("labforge_alerts_firing", "gauge", "Firing alerts by severity",
           [({"severity": s}, firing[s]) for s in ("critical", "warning", "info")])

    from labforge.rigs import overview

    rig = [({"pool": p["pool"], "state": st}, sum(1 for d in p["devices"] if d["rig"] == st))
           for p in overview(session) for st in ("free", "leased", "quarantined")]
    metric("labforge_rig_devices", "gauge", "Rig devices by pool and state", rig)
    results = Counter((lease.pool, lease.result) for lease in session.exec(select(Lease)) if lease.result)
    metric("labforge_rig_leases_total", "counter", "Returned leases by pool and result",
           [({"pool": pool, "result": res}, n) for (pool, res), n in sorted(results.items())])
    return "\n".join(lines) + "\n"


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(session: Session = Depends(get_session)) -> str:
    return render(session)
