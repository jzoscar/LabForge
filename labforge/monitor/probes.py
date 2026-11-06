"""Agentless async probes: ICMP, TCP, HTTP, Redfish/IPMI BMC health, docker container state."""

from __future__ import annotations

import asyncio
import os
import platform
import re
from dataclasses import dataclass

import httpx

from labforge.iac.providers.bmc import ipmi_argv, redfish_base
from labforge.iac.secrets import SecretResolver, env_key
from labforge.iac.spec import Bmc

_PING_TIME = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


@dataclass
class ProbeResult:
    probe: str
    ok: bool | None  # None = unknown (probe could not run)
    latency_ms: float | None = None
    detail: str = ""


def _now() -> float:
    return asyncio.get_running_loop().time()


async def _exec(argv: list[str], timeout: float, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def ping(host: str, timeout: float = 2.0, label: str = "ping") -> ProbeResult:
    system = platform.system()
    secs = str(max(1, int(timeout)))
    if system == "Windows":
        argv = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif system == "Darwin":
        argv = ["ping", "-c", "1", "-t", secs, host]
    else:
        argv = ["ping", "-c", "1", "-W", secs, host]
    start = _now()
    try:
        rc, out = await _exec(argv, timeout + 2)
    except FileNotFoundError:
        return ProbeResult(label, None, detail="ping not available")
    except TimeoutError:
        return ProbeResult(label, False, detail="timeout")
    m = _PING_TIME.search(out)
    latency = float(m.group(1)) if m else (_now() - start) * 1000
    if rc == 0:
        return ProbeResult(label, True, round(latency, 2), f"{host} reachable")
    return ProbeResult(label, False, detail=f"{host} unreachable")


async def tcp(host: str, port: int, timeout: float = 3.0, label: str | None = None) -> ProbeResult:
    label = label or f"tcp:{port}"
    start = _now()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError) as e:
        return ProbeResult(label, False, detail=f"{host}:{port} {type(e).__name__}: {e}".strip())
    latency = (_now() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return ProbeResult(label, True, round(latency, 2), f"{host}:{port} open")


async def http(url: str, timeout: float = 5.0, label: str | None = None) -> ProbeResult:
    label = label or url
    start = _now()
    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return ProbeResult(label, False, detail=f"{type(e).__name__}: {e}")
    latency = round((_now() - start) * 1000, 2)
    return ProbeResult(label, r.status_code < 400, latency, f"HTTP {r.status_code}")


async def redfish(bmc: Bmc, secrets: SecretResolver, desired_power: str | None, timeout: float = 10) -> ProbeResult:
    raw = secrets.get_optional(bmc.credentials_ref)
    if not raw or ":" not in raw:
        return ProbeResult("bmc", None, detail=f"no BMC credentials (set {env_key(bmc.credentials_ref)})")
    user, _, password = raw.partition(":")
    start = _now()
    try:
        async with httpx.AsyncClient(base_url=redfish_base(bmc), auth=(user, password),
                                     verify=bmc.verify_tls, timeout=timeout) as c:

            async def get(path: str) -> dict:
                r = await c.get(path)
                r.raise_for_status()
                return r.json()

            path = (f"/redfish/v1/Systems/{bmc.system_id}" if bmc.system_id
                    else (await get("/redfish/v1/Systems"))["Members"][0]["@odata.id"])
            system = await get(path)
            temp = await _redfish_max_temp(get, system)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        return ProbeResult("bmc", False, detail=f"redfish error: {type(e).__name__}: {e}")
    latency = round((_now() - start) * 1000, 2)

    power = system.get("PowerState", "Unknown")
    health = (system.get("Status") or {}).get("HealthRollup") or (system.get("Status") or {}).get("Health")
    parts = [f"power={power}", f"health={health or 'n/a'}"]
    if temp is not None:
        parts.append(f"maxTemp={temp:.0f}C")
    ok = health in (None, "OK") and (desired_power is None or power.lower() == desired_power)
    return ProbeResult("bmc", ok, latency, " ".join(parts))


async def _redfish_max_temp(get, system: dict) -> float | None:
    """Best effort: highest temperature reading on the first chassis."""
    try:
        chassis = system["Links"]["Chassis"][0]["@odata.id"]
        thermal = await get(f"{chassis}/Thermal")
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    readings = [t.get("ReadingCelsius") for t in thermal.get("Temperatures", [])]
    readings = [r for r in readings if isinstance(r, (int, float))]
    return max(readings) if readings else None


async def ipmi(bmc: Bmc, secrets: SecretResolver, desired_power: str | None, timeout: float = 10) -> ProbeResult:
    raw = secrets.get_optional(bmc.credentials_ref)
    if not raw or ":" not in raw:
        return ProbeResult("bmc", None, detail=f"no BMC credentials (set {env_key(bmc.credentials_ref)})")
    user, _, password = raw.partition(":")
    try:
        rc, out = await _exec(ipmi_argv(bmc, user) + ["chassis", "power", "status"], timeout,
                              env={**os.environ, "IPMI_PASSWORD": password})
    except FileNotFoundError:
        return ProbeResult("bmc", None, detail="ipmitool not installed")
    except TimeoutError:
        return ProbeResult("bmc", False, detail="ipmi timeout")
    if rc != 0:
        return ProbeResult("bmc", False, detail=out.strip()[-200:])
    power = "on" if out.strip().lower().endswith("on") else "off"
    ok = desired_power is None or power == desired_power
    return ProbeResult("bmc", ok, detail=f"power={power}")


async def docker_state(docker: list[str], name: str, timeout: float = 15, label: str = "docker") -> ProbeResult:
    try:
        rc, out = await _exec(docker + ["inspect", "-f", "{{.State.Status}}", name], timeout)
    except FileNotFoundError:
        return ProbeResult(label, None, detail="docker CLI not installed")
    except TimeoutError:
        return ProbeResult(label, None, detail="docker inspect timed out")
    state = out.strip().splitlines()[-1] if out.strip() else ""
    if rc != 0:
        return ProbeResult(label, False, detail=state[-200:] or "not found")
    return ProbeResult(label, state == "running", detail=f"state={state}")
