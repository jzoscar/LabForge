from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlmodel import Session, select

from conftest import ROOT
from labforge import service
from labforge.iac.spec import Host
from labforge.models import AgentReport, CheckResult, Event, ResourceState, utcnow
from labforge.monitor import probes
from labforge.monitor.probes import ProbeResult
from labforge.monitor.sweep import Monitor, agent_checks, aggregate


def test_aggregate():
    assert aggregate([]) == ("unknown", "")
    assert aggregate([ProbeResult("a", True), ProbeResult("b", None)])[0] == "up"
    assert aggregate([ProbeResult("a", True), ProbeResult("b", False, detail="x")]) == ("degraded", "b: x")
    assert aggregate([ProbeResult("a", False)])[0] == "down"


def test_tcp_probe_open_and_closed():
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        ok = await probes.tcp("127.0.0.1", port)
        server.close()
        await server.wait_closed()
        closed = await probes.tcp("127.0.0.1", port, timeout=1)
        return ok, closed

    ok, closed = asyncio.run(go())
    assert ok.ok and ok.latency_ms is not None
    assert closed.ok is False


def _host(**kw) -> Host:
    return Host.model_validate({"name": "h", "platform": {"os": "linux", "arch": "arm64"},
                                "monitor": {"agent": True}, **kw})


def _report(**kw) -> AgentReport:
    base = dict(address="e.host.h", ts=utcnow(), os="linux", arch="aarch64", cpu_percent=10, mem_percent=20,
                disk_percent=30, temp_c=50)
    return AgentReport(**{**base, **kw})


def test_agent_checks_ok_drift_stale_thresholds():
    now = utcnow()
    res = {r.probe: r for r in agent_checks(_host(), _report(), 120, now)}
    assert res["agent"].ok and res["thresholds"].ok and res["drift"].ok  # aarch64 == arm64

    drift = {r.probe: r for r in agent_checks(_host(), _report(arch="x86_64"), 120, now)}
    assert drift["drift"].ok is False and "x86_64" in drift["drift"].detail

    hot = {r.probe: r for r in agent_checks(_host(), _report(temp_c=92, disk_percent=97), 120, now)}
    assert hot["thresholds"].ok is False and "temp_c" in hot["thresholds"].detail

    stale = agent_checks(_host(), _report(ts=now - timedelta(minutes=10)), 120, now)
    assert [r.ok for r in stale] == [False]

    assert agent_checks(_host(), None, 120, now)[0].ok is False


def test_sweep_local_demo(engine, settings):
    run_id = service.create_run(engine, "test", dry_run=False)
    assert service.execute_run(engine, settings, run_id, ROOT / "examples" / "local", dry_run=False).status == "success"
    with Session(engine) as s:
        service.ingest_agent_report(s, {"env": "demo", "name": "this-mac", "os": "darwin", "arch": "arm64",
                                        "cpu_percent": 5, "mem_percent": 40, "disk_percent": 50})

    statuses = asyncio.run(Monitor(engine, settings).sweep())
    # nothing listens on tcp:1 -> down; this-mac has a fresh agent report but no server on :8000 -> degraded
    assert statuses["demo.host.offline-board"] == "down"
    assert statuses["demo.host.this-mac"] in {"up", "degraded"}

    with Session(engine) as s:
        checks = {c.probe for c in s.exec(select(CheckResult).where(CheckResult.address == "demo.host.this-mac"))}
        assert {"agent", "drift", "thresholds", "http:8000/api/v1/health"} <= checks
        assert s.exec(select(Event).where(Event.address == "demo.host.offline-board")).first().new_status == "down"
        assert s.exec(select(ResourceState).where(ResourceState.address == "demo.host.offline-board")).one().status == "down"
