"""Device test rigs: CI checks devices out of a pool, tests on them, and returns them.

Rules (milestone 2):
- A device belongs to a pool through its IaC label `pool: <name>`.
- Checkout health-checks a device *before* handing it out, with the same probes the
  monitor uses. A device that fails is power-cycled through its BMC and checked again
  ("fix, don't just file"); if it is still unhealthy, the next device is tried.
- Two consecutive failed health checks quarantine a device until a person clears it.
  Those failures are infrastructure failures, never test failures.
- Returning a device puts it back to a known-good state, whatever the result: its known-good
  firmware is flashed back if a test left another build on it, then it is power-cycled
  (BMC, or the PDU outlet for boards without a BMC).
- Leases expire, so a crashed CI job cannot hold a device forever.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from labforge.iac.providers.bmc import hard_reset
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Host
from labforge.models import DeviceHealth, Event, Lease, ResourceState, utcnow
from labforge.monitor.probes import ProbeResult
from labforge.monitor.sweep import Target, check_target

POOL_LABEL = "pool"
QUARANTINE_AFTER = 2
RESULTS = {"passed", "failed", "infra-failed"}

_checkout_lock = threading.Lock()


class RigError(Exception):
    def __init__(self, message: str, status: int = 400, **detail):
        self.status = status
        self.detail = {"error": message, **detail}
        super().__init__(message)


@dataclass
class Health:
    healthy: bool
    summary: str
    results: list[ProbeResult]


def pool_of(state: ResourceState) -> str | None:
    return state.spec.get("labels", {}).get(POOL_LABEL) if state.kind == "host" else None


def pool_members(session: Session, pool: str | None = None) -> list[ResourceState]:
    hosts = session.exec(select(ResourceState).where(ResourceState.kind == "host").order_by(ResourceState.address))
    return [h for h in hosts if pool_of(h) and (pool is None or pool_of(h) == pool)]


def _health_row(session: Session, address: str) -> DeviceHealth:
    row = session.exec(select(DeviceHealth).where(DeviceHealth.address == address)).first()
    return row or DeviceHealth(address=address)


def _active_lease(session: Session, address: str) -> Lease | None:
    return session.exec(select(Lease).where(Lease.address == address, Lease.status == "active")).first()


# --- health ---------------------------------------------------------------


def check(state: ResourceState, secrets: SecretResolver) -> Health:
    host = Host.model_validate(state.spec)
    target = Target(state.address, state.env, state.kind, state.name, state.spec)
    results = asyncio.run(check_target(target, {state.address: host}, secrets))
    failing = [r for r in results if r.ok is False]
    passing = [r for r in results if r.ok]
    healthy = bool(passing) and not failing
    summary = "; ".join(f"{r.probe}: {r.detail}" for r in failing) or "all probes passed"
    return Health(healthy, summary[:300], results)


def _record(session: Session, state: ResourceState, healthy: bool, reason: str) -> DeviceHealth:
    row = _health_row(session, state.address)
    if healthy:
        row.consecutive_failures = 0
    else:
        row.consecutive_failures += 1
        if row.consecutive_failures >= QUARANTINE_AFTER and not row.quarantined:
            row.quarantined, row.reason = True, reason
            session.add(Event(address=state.address, old_status=state.status, new_status="quarantined",
                              detail=f"quarantined after {row.consecutive_failures} failed health checks: {reason}"))
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    return row


def _heal(state: ResourceState, secrets: SecretResolver, wait_seconds: float) -> tuple[Health | None, str]:
    """Power-cycle (BMC, else PDU) and wait for the device to come back healthy."""
    try:
        how = hard_reset(Host.model_validate(state.spec), secrets)
    except Exception as e:  # nothing to reset with, or the BMC/PDU refused: an infrastructure failure
        return None, f"power cycle failed: {e}"
    deadline = time.monotonic() + wait_seconds
    health = check(state, secrets)
    while not health.healthy and time.monotonic() < deadline:
        time.sleep(0.5)
        health = check(state, secrets)
    return health, how


# --- checkout / release -----------------------------------------------------


def device_endpoints(spec: dict) -> dict[str, str]:
    """Where to reach a device: its HTTP API (from the first http probe) and its BMC."""
    host = spec.get("address")
    probes = spec.get("monitor", {}).get("probes") or []
    http_port = next((p.get("port") for p in probes if p.get("type") == "http" and p.get("port")), None)
    endpoints = {"http": f"http://{host}:{http_port}" if host and http_port else None,
                 "bmc": (spec.get("bmc") or {}).get("address")}
    return {k: v for k, v in endpoints.items() if v}


def lease_view(lease: Lease, state: ResourceState | None) -> dict:
    spec = state.spec if state else {}
    return {
        "leaseId": lease.id, "pool": lease.pool, "holder": lease.holder, "status": lease.status,
        "result": lease.result, "device": state.name if state else lease.address.rsplit(".", 1)[-1],
        "address": lease.address, "host": spec.get("address"), "endpoints": device_endpoints(spec),
        "platform": spec.get("platform"), "labels": spec.get("labels", {}),
        "acquiredAt": lease.acquired_at, "expiresAt": lease.expires_at, "releasedAt": lease.released_at,
        "queuedSeconds": lease.queued_seconds, "firmware": lease.firmware_version, "note": lease.note,
    }


def checkout(engine: Engine, pool: str, holder: str, *, ttl_seconds: int = 1800, purpose: str = "",
             secrets: SecretResolver | None = None, heal_wait: float = 15.0,
             queued_seconds: float | None = None) -> dict:
    secrets = secrets or SecretResolver()
    if not holder.strip():
        raise RigError("holder is required (e.g. the CI job URL)")
    # One checkout at a time, so two jobs never get the same device.
    with _checkout_lock, Session(engine) as session:
        members = pool_members(session, pool)
        if not members:
            raise RigError(f"unknown or empty pool {pool!r}", 404)
        free = []
        busy = quarantined = 0
        for m in members:
            if _health_row(session, m.address).quarantined:
                quarantined += 1
            elif _active_lease(session, m.address):
                busy += 1
            else:
                free.append(m)
        # Devices the monitor already sees as up go first.
        free.sort(key=lambda m: (m.status != "up", m.name))

        tried = []
        for state in free:
            health = check(state, secrets)
            note = ""
            if not health.healthy:
                row = _record(session, state, False, health.summary)
                if not row.quarantined:
                    healed, how = _heal(state, secrets, heal_wait)
                    note = f"failed health check ({health.summary}); {how}"
                    if healed is not None:
                        health = healed
                if not health.healthy:
                    _record(session, state, False, health.summary)
                    tried.append({"device": state.name, "reason": health.summary,
                                  "quarantined": _health_row(session, state.address).quarantined})
                    continue
            _record(session, state, True, "")
            lease = Lease(address=state.address, pool=pool, holder=holder.strip(), purpose=purpose,
                          expires_at=utcnow() + timedelta(seconds=ttl_seconds), queued_seconds=queued_seconds,
                          note=(note + "; healthy after recovery") if note else "")
            session.add(lease)
            session.commit()
            session.refresh(lease)
            return {**lease_view(lease, state), "tried": tried}

        raise RigError(f"no healthy device available in pool {pool!r}", 409,
                       busy=busy, quarantined=quarantined, tried=tried)


def release(engine: Engine, lease_id: int, result: str, *, secrets: SecretResolver | None = None,
            restore: bool = True) -> dict:
    if result not in RESULTS:
        raise RigError(f"result must be one of {sorted(RESULTS)}")
    secrets = secrets or SecretResolver()
    with Session(engine) as session:
        lease = session.get(Lease, lease_id)
        if not lease:
            raise RigError(f"lease {lease_id} not found", 404)
        if lease.status != "active":
            raise RigError(f"lease {lease_id} is already {lease.status}", 409)
        state = session.exec(select(ResourceState).where(ResourceState.address == lease.address)).first()
        notes = []
        if state and result == "infra-failed":
            _record(session, state, False, f"reported infra-failed by {lease.holder}")
            notes.append("infrastructure failure recorded")
        if restore and state:
            notes += _restore(state, lease, secrets)
        lease.status, lease.result, lease.released_at = "released", result, utcnow()
        lease.note = "; ".join(filter(None, [lease.note, *notes]))
        session.add(lease)
        session.commit()
        session.refresh(lease)
        return lease_view(lease, state)


def _restore(state: ResourceState, lease: Lease, secrets: SecretResolver) -> list[str]:
    """Known-good firmware back on (if the lease flashed something else), then a power cycle."""
    host = Host.model_validate(state.spec)
    notes = []
    known_good = host.firmware.known_good if host.firmware else None
    if known_good and lease.firmware_version and lease.firmware_version != known_good:
        try:
            _install(host, state, known_good, None, None, secrets)
            notes.append(f"restored known-good firmware {known_good}")
        except Exception as e:
            notes.append(f"restoring firmware {known_good} failed: {e}")
    try:
        notes.append("restored: " + hard_reset(host, secrets))
    except Exception as e:
        notes.append(f"not power-cycled: {e}")
    return notes


def _leased(session: Session, lease_id: int) -> tuple[Lease, ResourceState]:
    lease = session.get(Lease, lease_id)
    if not lease or lease.status != "active":
        raise RigError(f"lease {lease_id} is not active", 409)
    state = session.exec(select(ResourceState).where(ResourceState.address == lease.address)).first()
    if state is None:
        raise RigError(f"device {lease.address} is no longer declared", 409)
    return lease, state


def _device_url(state: ResourceState) -> str:
    url = device_endpoints(state.spec).get("http")
    if not url:
        raise RigError(f"{state.name} has no HTTP endpoint (declare an http probe with a port)", 409)
    return url


def _device_version(url: str) -> dict | None:
    try:
        r = httpx.get(f"{url}/version", timeout=5)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _install(host: Host, state: ResourceState, version: str, sha256: str | None, size: int | None,
             secrets: SecretResolver) -> None:
    method = host.firmware.method if host.firmware else "http"
    if method == "command":
        import shlex
        import subprocess

        assert host.firmware and host.firmware.command
        cmd = host.firmware.command.format(address=host.address or "", version=version, sha256=sha256 or "")
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            raise RigError(f"flash failed: `{cmd}` exited {p.returncode}: {(p.stderr or p.stdout).strip()[-200:]}", 502)
        return
    url = _device_url(state)
    try:
        r = httpx.post(f"{url}/firmware", json={"version": version, "sha256": sha256, "size": size}, timeout=30)
    except httpx.HTTPError as e:
        raise RigError(f"flash failed: {state.name} unreachable: {e}", 502) from e
    if r.status_code >= 400:
        raise RigError(f"flash failed: {state.name} answered HTTP {r.status_code}", 502)


def device_status(engine: Engine, lease_id: int, secrets: SecretResolver | None = None) -> dict:
    """Health and running firmware of a leased device."""
    with Session(engine) as session:
        lease, state = _leased(session, lease_id)
        health = check(state, secrets or SecretResolver())
        return {"leaseId": lease.id, "device": state.name, "healthy": health.healthy, "summary": health.summary,
                "version": _device_version(_device_url(state))}


def flash(engine: Engine, lease_id: int, version: str, *, sha256: str | None = None, size: int | None = None,
          secrets: SecretResolver | None = None, wait_seconds: float = 60.0) -> dict:
    """Install a firmware build on a leased device and wait until it runs healthy.

    How the image gets there is declared per device (`firmware.method`): the device's own firmware
    endpoint (`POST <device>/firmware`), or a command run on the LabForge server - a vendor update
    tool, a PXE re-image script, a serial flasher. CI never needs device credentials.
    """
    secrets = secrets or SecretResolver()
    with Session(engine) as session:
        lease, state = _leased(session, lease_id)
        url = _device_url(state)
        started = time.monotonic()
        _install(Host.model_validate(state.spec), state, version, sha256, size, secrets)

        deadline = started + wait_seconds
        running, health = None, None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            running = _device_version(url)
            if running and running.get("firmware") == version:
                health = check(state, secrets)
                if health.healthy:
                    break
        ok = bool(running and running.get("firmware") == version and health and health.healthy)
        lease.note = "; ".join(filter(None, [lease.note, f"flashed {version}" + (f" (sha256 {sha256[:12]})" if sha256 else "")
                                             + ("" if ok else " - device did not come back healthy")]))
        session.add(lease)
        session.commit()
        if ok:
            lease.firmware_version, lease.firmware_sha256 = version, sha256
            session.add(lease)
            session.commit()
        if not ok:
            raise RigError(f"{state.name} did not come back healthy on {version} within {wait_seconds:.0f}s", 504,
                           device=state.name, running=running, health=health.summary if health else None)
        return {"leaseId": lease.id, "device": state.name, "version": version, "sha256": sha256,
                "running": running, "seconds": round(time.monotonic() - started, 2)}


def renew(engine: Engine, lease_id: int, ttl_seconds: int) -> dict:
    with Session(engine) as session:
        lease = session.get(Lease, lease_id)
        if not lease or lease.status != "active":
            raise RigError(f"lease {lease_id} is not active", 409)
        lease.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        session.add(lease)
        session.commit()
        session.refresh(lease)
        state = session.exec(select(ResourceState).where(ResourceState.address == lease.address)).first()
        return lease_view(lease, state)


def unquarantine(engine: Engine, address: str) -> None:
    with Session(engine) as session:
        row = session.exec(select(DeviceHealth).where(DeviceHealth.address == address)).first()
        if not row or not row.quarantined:
            raise RigError(f"{address} is not quarantined", 409)
        row.quarantined, row.consecutive_failures, row.reason, row.updated_at = False, 0, "", utcnow()
        session.add(row)
        session.add(Event(address=address, old_status="quarantined", new_status="unknown",
                          detail="released from quarantine"))
        session.commit()


def expire_leases(session: Session) -> int:
    now = utcnow()
    expired = list(session.exec(select(Lease).where(Lease.status == "active", Lease.expires_at < now)))
    for lease in expired:
        lease.status, lease.released_at = "expired", now
        lease.note = "; ".join(filter(None, [lease.note, "lease expired; device returned to the pool"]))
        session.add(lease)
    return len(expired)


def overview(session: Session) -> list[dict]:
    """Pools with each device's rig state: free | leased | quarantined."""
    pools: dict[str, list] = {}
    for state in pool_members(session):
        health = _health_row(session, state.address)
        lease = _active_lease(session, state.address)
        rig_state = "quarantined" if health.quarantined else ("leased" if lease else "free")
        pools.setdefault(pool_of(state), []).append(
            {"state": state, "rig": rig_state, "lease": lease, "health": health})
    return [{"pool": name, "devices": devices,
             "free": sum(1 for d in devices if d["rig"] == "free" and d["state"].status == "up")}
            for name, devices in sorted(pools.items())]


def trends(session: Session, days: int = 7) -> list[dict]:
    """Per pool over the last `days`: how busy each device was, how jobs ended, how long they waited."""
    since = utcnow() - timedelta(days=days)
    window = days * 86400
    leases = list(session.exec(select(Lease).where(Lease.acquired_at >= since)))
    now = utcnow()
    out = []
    for pool in sorted({pool_of(m) for m in pool_members(session)}):
        mine = [lease for lease in leases if lease.pool == pool]
        held: dict[str, float] = {}
        for lease in mine:
            end = lease.released_at or now
            held[lease.address] = held.get(lease.address, 0.0) + (end - lease.acquired_at).total_seconds()
        results = {r: sum(1 for lease in mine if lease.result == r) for r in sorted(RESULTS)}
        waits = [lease.queued_seconds for lease in mine if lease.queued_seconds is not None]
        out.append({
            "pool": pool, "leases": len(mine), "results": results,
            "infraShare": round(results["infra-failed"] / max(1, sum(results.values())), 3),
            "avgWaitSeconds": round(sum(waits) / len(waits), 1) if waits else 0.0,
            "maxWaitSeconds": round(max(waits), 1) if waits else 0.0,
            "utilisation": {m.name: round(held.get(m.address, 0.0) / window, 4)
                            for m in pool_members(session, pool)},
            "heldSeconds": {m.name: round(held.get(m.address, 0.0), 3) for m in pool_members(session, pool)},
        })
    return out
