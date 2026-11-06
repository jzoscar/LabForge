"""Periodic health sweep over every resource recorded in IaC state.

Combines agentless probes with agent reports, and flags drift between what the
agent sees on the machine and what the IaC spec declares (e.g. ARM board
re-imaged with an x86 image, Windows box swapped).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from labforge.config import Settings
from labforge.iac.providers.remote import docker_argv
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Container, Host, LoadBalancer, Network, Probe
from labforge.models import AgentReport, CheckResult, Event, ResourceState, utcnow
from labforge.monitor import probes
from labforge.monitor.probes import ProbeResult

log = logging.getLogger("labforge.monitor")

ARCH_ALIASES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64",
                "armv7l": "armv7", "armv7": "armv7", "riscv64": "riscv64"}
OS_ALIASES = {"darwin": "macos"}
THRESHOLDS = {"cpu_percent": 95, "mem_percent": 95, "disk_percent": 90, "temp_c": 85}


def normalize_arch(arch: str | None) -> str:
    a = (arch or "").lower()
    return ARCH_ALIASES.get(a, a)


@dataclass
class Target:
    address: str
    env: str
    kind: str
    name: str
    spec: dict


def aggregate(results: list[ProbeResult]) -> tuple[str, str]:
    known = [r for r in results if r.ok is not None]
    failing = [r for r in results if r.ok is False]
    detail = "; ".join(f"{r.probe}: {r.detail}" for r in failing)[:500]
    if not known:
        return "unknown", "; ".join(f"{r.probe}: {r.detail}" for r in results)[:500]
    if not failing:
        return "up", ""
    if len(failing) == len(known):
        return "down", detail
    return "degraded", detail


def agent_checks(host: Host, report: AgentReport | None, stale_after: int, now: datetime) -> list[ProbeResult]:
    if not host.monitor.agent:
        return []
    if report is None:
        return [ProbeResult("agent", False, detail="no report received yet")]
    age = (now - report.ts).total_seconds()
    if age > stale_after:
        return [ProbeResult("agent", False, detail=f"last report {int(age)}s ago")]

    summary = f"cpu {report.cpu_percent:.0f}% mem {report.mem_percent:.0f}%"
    if report.disk_percent is not None:
        summary += f" disk {report.disk_percent:.0f}%"
    if report.temp_c is not None:
        summary += f" temp {report.temp_c:.0f}C"
    out = [ProbeResult("agent", True, detail=summary)]

    t = host.monitor.thresholds
    limits = {**THRESHOLDS, **{k: v for k, v in {"cpu_percent": t.cpu, "mem_percent": t.mem,
                                                    "disk_percent": t.disk, "temp_c": t.temp}.items() if v is not None}}
    over = [f"{k}={v:.0f} (>{limits[k]:.0f})" for k in limits
            if (v := getattr(report, k)) is not None and v > limits[k]]
    extra = report.payload.get("extra") or {}
    docker_disk = (extra.get("docker") or {}).get("disk_percent")
    if docker_disk is not None and docker_disk > limits["disk_percent"]:
        over.append(f"docker_disk={docker_disk:.0f} (>{limits['disk_percent']:.0f})")
    out.append(ProbeResult("thresholds", not over, detail=", ".join(over) or "within limits"))

    running = extra.get("processes") or {}
    for name in host.monitor.processes:
        if name not in running:
            out.append(ProbeResult(f"process:{name}", None, detail="agent does not watch it (run with --process)"))
        else:
            out.append(ProbeResult(f"process:{name}", bool(running[name]),
                                   detail="running" if running[name] else "not running"))

    drift = []
    if report.arch and normalize_arch(report.arch) != host.platform.arch:
        drift.append(f"arch {report.arch} != spec {host.platform.arch}")
    if report.os and OS_ALIASES.get(report.os.lower(), report.os.lower()) != host.platform.os:
        drift.append(f"os {report.os} != spec {host.platform.os}")
    out.append(ProbeResult("drift", not drift, detail="; ".join(drift) or "matches IaC"))
    return out


def _probe(p: Probe, address: str):
    if p.type == "ping":
        return probes.ping(address, p.timeout)
    if p.type == "tcp":
        return probes.tcp(address, p.port or 0, p.timeout, label=p.label)
    url = f"{'https' if p.https else 'http'}://{address}{f':{p.port}' if p.port else ''}{p.path}"
    return probes.http(url, p.timeout, label=p.label)


async def check_target(t: Target, hosts: dict[str, Host], secrets: SecretResolver) -> list[ProbeResult]:
    coros = []
    if t.kind == "host":
        h = Host.model_validate(t.spec)
        # A host declared powered-off is only checked out-of-band.
        if h.address and h.power != "off":
            for p in h.monitor.probes if h.monitor.probes is not None else [Probe(type="ping")]:
                coros.append(_probe(p, h.address))
        if h.bmc:
            check = probes.redfish if h.bmc.type == "redfish" else probes.ipmi
            coros.append(check(h.bmc, secrets, h.power))
    elif t.kind == "container":
        c = Container.model_validate(t.spec)
        host = hosts.get(f"{t.env}.host.{c.host}")
        if host:
            for name in c.instance_names:
                coros.append(probes.docker_state(docker_argv(host), name, label=f"docker:{name}"
                                                 if c.replicas > 1 else "docker"))
    elif t.kind == "network":
        n = Network.model_validate(t.spec)
        coros += [probes.ping(s.gateway, label=f"gw:{s.name}") for s in n.subnets if s.gateway]
    elif t.kind == "loadbalancer":
        lb = LoadBalancer.model_validate(t.spec)
        if lb.vip:
            coros += [probes.tcp(lb.vip, ls.port, label=f"vip:{ls.port}") for ls in lb.listeners]
        hc = lb.health_check
        for name in lb.targets if hc else []:
            host = hosts.get(f"{t.env}.host.{name}")
            if not host or not host.address:
                continue
            if hc.protocol == "http":
                coros.append(probes.http(f"http://{host.address}:{hc.port}{hc.path}", label=f"target:{name}"))
            else:
                coros.append(probes.tcp(host.address, hc.port, label=f"target:{name}"))
    return list(await asyncio.gather(*coros))


class Monitor:
    def __init__(self, engine: Engine, settings: Settings, secrets: SecretResolver | None = None):
        self.engine = engine
        self.settings = settings
        self.secrets = secrets or SecretResolver()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("monitor sweep failed")
            await asyncio.sleep(self.settings.monitor_interval)

    def _load(self) -> tuple[list[Target], dict[str, AgentReport]]:
        with Session(self.engine) as s:
            states = s.exec(select(ResourceState)).all()
            targets = [Target(st.address, st.env, st.kind, st.name, st.spec) for st in states]
            reports: dict[str, AgentReport] = {}
            for t in targets:
                if t.kind == "host":
                    r = s.exec(select(AgentReport).where(AgentReport.address == t.address)
                               .order_by(AgentReport.ts.desc()).limit(1)).first()
                    if r:
                        reports[t.address] = r
        return targets, reports

    async def sweep(self) -> dict[str, str]:
        targets, reports = await asyncio.to_thread(self._load)
        hosts = {t.address: Host.model_validate(t.spec) for t in targets if t.kind == "host"}
        now = utcnow()
        sem = asyncio.Semaphore(64)

        async def one(t: Target) -> tuple[Target, list[ProbeResult]]:
            async with sem:
                try:
                    res = await check_target(t, hosts, self.secrets)
                except Exception as e:  # a broken spec or probe must not stop the sweep
                    res = [ProbeResult("error", None, detail=f"{type(e).__name__}: {e}")]
            if t.kind == "host":
                res += agent_checks(hosts[t.address], reports.get(t.address), self.settings.agent_stale_after, now)
            return t, res

        results = await asyncio.gather(*(one(t) for t in targets))
        return await asyncio.to_thread(self._persist, results)

    def _alerts(self, session: Session) -> None:
        from labforge.alerting import evaluate
        from labforge.iac.loader import load_policies

        try:
            policies = load_policies(self.settings.workspace().root)
            evaluate(session, policies, base_url=self.settings.public_url, secrets=self.secrets)
        except Exception:  # alerting must never stop monitoring
            log.exception("alert evaluation failed")
            session.rollback()

    def _persist(self, results: list[tuple[Target, list[ProbeResult]]]) -> dict[str, str]:
        now = utcnow()
        statuses: dict[str, str] = {}
        with Session(self.engine) as s:
            for t, res in results:
                st = s.exec(select(ResourceState).where(ResourceState.address == t.address)).first()
                if st is None:  # removed by a concurrent apply
                    continue
                existing = {c.probe: c for c in s.exec(select(CheckResult).where(CheckResult.address == t.address))}
                for r in res:
                    row = existing.pop(r.probe, None) or CheckResult(address=t.address, probe=r.probe)
                    row.ok, row.latency_ms, row.detail, row.ts = r.ok, r.latency_ms, r.detail[:1000], now
                    s.add(row)
                for stale in existing.values():
                    s.delete(stale)

                status, detail = aggregate(res)
                if status != st.status:
                    s.add(Event(address=t.address, old_status=st.status, new_status=status, detail=detail))
                st.status, st.status_detail, st.checked_at = status, detail, now
                s.add(st)
                statuses[t.address] = status

            from labforge.rigs import expire_leases  # local import: rigs builds on this module

            expire_leases(s)
            s.commit()
            self._alerts(s)
            keep = timedelta(hours=self.settings.retention_hours)
            s.exec(delete(AgentReport).where(AgentReport.ts < now - keep))
            s.exec(delete(Event).where(Event.ts < now - keep * 7))
            s.commit()
        return statuses
