#!/usr/bin/env python3
"""LabForge host agent.

Self-contained (stdlib + psutil) so it can be copied onto ARM Linux boards and
Windows machines without installing the full platform:

    pip install psutil
    python agent.py --server http://labforge:8000 --env dev --name arm-node-01 --token $TOKEN

The --env/--name pair must match the host declared in IaC.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request

import psutil


def _max_temp() -> float | None:
    temps: list[float] = []
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors:
        try:
            for entries in sensors().values():
                temps += [e.current for e in entries if e.current]
        except Exception:
            pass
    if not temps:
        # Many ARM boards only expose the kernel thermal zones.
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path) as f:
                    temps.append(int(f.read().strip()) / 1000)
            except (OSError, ValueError):
                pass
    return round(max(temps), 1) if temps else None


def _processes(names: list[str]) -> dict[str, bool]:
    """Is each named process running? A name matches the process name, or the file name (with or
    without extension) of one of its arguments - so "actions-runner", "Runner.Listener" and
    "bamboo-agent" (java -jar bamboo-agent.jar) all work, but text inside a shell command does not."""
    wanted = {n.lower(): n for n in names}
    running = {n: False for n in names}
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.info["pid"] == me:
            continue
        try:
            words = {(proc.info["name"] or "").lower()}
            for arg in proc.info["cmdline"] or []:
                base = os.path.basename(arg).lower()
                words |= {base, os.path.splitext(base)[0]}
        except (psutil.Error, TypeError):
            continue
        for key in wanted.keys() & words:
            running[wanted[key]] = True
    return running


def _docker() -> dict | None:
    """Docker disk usage on build machines: images, containers, build cache, and the data volume."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return None
    try:
        out = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-200:]}
    usage = {}
    for line in out.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        usage[row.get("Type", "?")] = {"size": row.get("Size"), "reclaimable": row.get("Reclaimable")}
    data_root = "/var/lib/docker"
    if os.path.isdir(data_root):
        usage["disk_percent"] = psutil.disk_usage(data_root).percent
    return usage


def collect(env: str, name: str, processes: list[str] | None = None) -> dict:
    root = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    try:
        load = [round(x, 2) for x in psutil.getloadavg()]
    except (AttributeError, OSError):
        load = None
    return {
        "env": env,
        "name": name,
        "hostname": socket.gethostname(),
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "mem_percent": vm.percent,
        "disk_percent": psutil.disk_usage(root).percent,
        "temp_c": _max_temp(),
        "extra": {
            "os_version": platform.platform(),
            "cpu_count": psutil.cpu_count(),
            "load": load,
            "mem_total": vm.total,
            "uptime_s": int(time.time() - psutil.boot_time()),
            "net_bytes_sent": net.bytes_sent if net else None,
            "net_bytes_recv": net.bytes_recv if net else None,
            "python": platform.python_version(),
            "processes": _processes(processes or []),
            "docker": _docker(),
        },
    }


def post(server: str, token: str | None, payload: dict, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        server.rstrip("/") + "/api/v1/agent/report",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"X-LabForge-Token": token} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LabForge host agent")
    ap.add_argument("--server", default=os.environ.get("LABFORGE_SERVER", "http://127.0.0.1:8000"))
    ap.add_argument("--env", default=os.environ.get("LABFORGE_ENV", "dev"))
    ap.add_argument("--name", default=os.environ.get("LABFORGE_HOST") or socket.gethostname().lower())
    ap.add_argument("--token", default=os.environ.get("LABFORGE_AGENT_TOKEN"))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true", help="send one report and exit")
    ap.add_argument("--process", action="append", default=[], metavar="NAME",
                    help="report whether a process is running, e.g. --process actions-runner (repeatable)")
    args = ap.parse_args(argv)

    backoff = args.interval
    while True:
        try:
            resp = post(args.server, args.token, collect(args.env, args.name, args.process))
            if not resp.get("managed", True):
                print(f"warning: {args.env}.host.{args.name} is not declared in IaC", file=sys.stderr)
            backoff = args.interval
            if args.once:
                return 0
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"report failed: {e}", file=sys.stderr)
            if args.once:
                return 1
            backoff = min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
