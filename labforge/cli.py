"""labforge CLI: validate / plan / apply / destroy / state / monitor / serve / agent."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from sqlmodel import Session, select

from labforge.config import Settings
from labforge.db import make_engine
from labforge.iac.loader import LoadError, load_path
from labforge.iac.planner import Plan
from labforge.models import ResourceState

app = typer.Typer(help="LabForge: IaC for lab hardware, servers, containers and networks.", no_args_is_help=True)

ACTION_STYLE = {"create": ("+", "green"), "update": ("~", "yellow"), "delete": ("-", "red"), "noop": ("=", "bright_black")}
STATUS_COLOR = {"up": "green", "degraded": "yellow", "down": "red", "unknown": "bright_black"}

PathArg = typer.Argument(None, help="IaC file or directory (default: $LABFORGE_IAC_PATH or ./infra)")
ServerOpt = typer.Option("http://127.0.0.1:8000", "--server", envvar="LABFORGE_SERVER")
TokenOpt = typer.Option(None, "--token", envvar="LABFORGE_API_TOKEN")


def _settings() -> Settings:
    return Settings.from_env()


def _path(path: Path | None, settings: Settings) -> Path:
    return path or settings.iac_path


def _load_or_exit(path: Path):
    try:
        return load_path(path)
    except LoadError as e:
        typer.secho(f"✗ {path}: {len(e.errors)} error(s)", fg="red", err=True)
        for err in e.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(1) from e


def _print_plan(plan: Plan, show_noop: bool = False) -> None:
    for c in plan.changes:
        if c.action == "noop" and not show_noop:
            continue
        sym, color = ACTION_STYLE[c.action]
        tier = typer.style(f" [{c.tier}]", fg="red") if c.tier == "prod" else ""
        diff = typer.style(f"  ({', '.join(c.diff)})", fg="bright_black") if c.diff else ""
        typer.echo(typer.style(f"  {sym} {c.address}", fg=color) + tier + diff)
    n = plan.counts()
    typer.echo(f"\nPlan: {n['create']} to create, {n['update']} to update, {n['delete']} to delete, {n['noop']} unchanged.")


@app.command()
def validate(path: Path | None = PathArg) -> None:
    """Validate IaC files (schema + cross references)."""
    from labforge.iac.loader import load_policies

    p = _path(path, _settings())
    docs = _load_or_exit(p)
    for policy in load_policies(p):
        s = policy.spec
        typer.secho(f"✓ alert policy {policy.metadata.name}: {len(s.channels)} channels, {len(s.routes)} routes", fg="green")
    for d in docs:
        s = d.spec
        typer.secho(f"✓ {d.metadata.name} [{d.metadata.tier}]: {len(s.networks)} networks, "
                    f"{len(s.load_balancers)} LBs, {len(s.hosts)} hosts, {len(s.containers)} containers", fg="green")


@app.command()
def plan(path: Path | None = PathArg, all: bool = typer.Option(False, "--all", help="also show unchanged"),
         format: str = typer.Option("text", "--format", help="text | markdown (for PR comments / CI summaries)"),
         remote: str | None = typer.Option(None, "--remote", envvar="LABFORGE_REMOTE",
                                           help="plan against a LabForge server's state (GitOps)"),
         token: str | None = TokenOpt) -> None:
    """Show what apply would change - locally, or against a remote LabForge's real state."""
    from labforge.iac.planner import build_plan, plan_markdown

    settings = _settings()
    p = _path(path, settings)
    docs = _load_or_exit(p)
    if remote:
        files = _local_files(p)
        r = _rig_request("POST", remote, "/iac/plan", token, json={"files": files})
        if format == "markdown":
            typer.echo(r["markdown"])
        else:
            for c in r["changes"]:
                if c["action"] != "noop" or all:
                    sym, color = ACTION_STYLE[c["action"]]
                    typer.echo(typer.style(f"  {sym} {c['address']}", fg=color))
            n = r["counts"]
            typer.echo(f"\nPlan: {n['create']} to create, {n['update']} to update, {n['delete']} to delete, {n['noop']} unchanged.")
        return
    with Session(make_engine(settings.db_url)) as s:
        result = build_plan(docs, list(s.exec(select(ResourceState))))
    if format == "markdown":
        typer.echo(plan_markdown(result))
    else:
        _print_plan(result, show_noop=all)


def _local_files(path: Path) -> dict[str, str]:
    from labforge.iac.loader import iac_files

    root = path if path.is_dir() else path.parent
    return {f.relative_to(root).as_posix(): f.read_text(encoding="utf-8") for f in iac_files(path)}


@app.command()
def sync(path: Path | None = PathArg,
         remote: str = typer.Option(..., "--remote", envvar="LABFORGE_REMOTE", help="LabForge server URL"),
         prune: bool = typer.Option(False, "--prune", help="delete server files that are not in PATH"),
         apply_: bool = typer.Option(False, "--apply", help="apply after syncing and wait for the result"),
         dry_run: bool = typer.Option(False, "--dry-run", help="with --apply: dry-run only"),
         token: str | None = TokenOpt) -> None:
    """GitOps: make a LabForge server's IaC workspace match PATH (a git checkout), then optionally apply."""
    import time

    local = _local_files(_path(path, _settings()))
    remote_files = {f["path"]: f for f in _rig_request("GET", remote, "/iac/files", token)}
    for rel, text in sorted(local.items()):
        r = _rig_request("PUT", remote, f"/iac/files/{rel}", token, content=text.encode(),
                         headers={"Content-Type": "text/plain"})
        changed = rel not in remote_files or remote_files[rel]["digest"] != r["digest"]
        typer.echo(f"{'~' if changed else '='} {rel}" + (f"  ({len(r['problems'])} problems)" if r["problems"] else ""))
    for rel in sorted(set(remote_files) - set(local)):
        if prune:
            _rig_request("DELETE", remote, f"/iac/files/{rel}", token)
            typer.echo(f"- {rel}")
        else:
            typer.secho(f"! {rel} exists only on the server (use --prune to delete it)", fg="yellow")
    if not apply_:
        return
    run_id = _rig_request("POST", remote, "/iac/apply", token, json={"dry_run": dry_run})["runId"]
    typer.echo(f"run #{run_id} started")
    printed = 0
    while True:
        run = _rig_request("GET", remote, f"/runs/{run_id}", None)
        lines = run["log"].splitlines()
        for line in lines[printed:]:
            typer.echo(line)
        printed = len(lines)
        if run["status"] != "running":
            break
        time.sleep(1)
    if run["status"] != "success":
        typer.secho(f"run #{run_id} {run['status']}", fg="red", err=True)
        raise typer.Exit(1)


def _run(path: Path, *, dry_run: bool, allow_prod: bool, yes: bool, destroy_envs: tuple[str, ...] = ()) -> None:
    from labforge import service

    settings = _settings()
    if not destroy_envs:
        _load_or_exit(path)
    engine = make_engine(settings.db_url)
    with Session(engine) as s:
        try:
            _, p = service.make_plan(s, path, destroy_envs)
        except LoadError as e:
            for err in e.errors:
                typer.echo(f"  - {err}", err=True)
            raise typer.Exit(1) from e
    _print_plan(p)
    if not p.has_changes:
        typer.secho("No changes.", fg="green")
        return
    if not dry_run and not yes:
        typer.confirm("\nApply these changes?", abort=True)

    from labforge.auth import audit, cli_actor

    action = "destroy" if destroy_envs else "apply"
    audit(engine, cli_actor(), f"{action}.start", str(path), f"dry_run={dry_run}", "cli")
    run_id = service.create_run(engine, f"cli:{path}", dry_run, action)
    typer.echo("")
    run = service.execute_run(engine, settings, run_id, path, dry_run=dry_run, allow_prod=allow_prod,
                              destroy_envs=destroy_envs, echo=typer.echo)
    if run.status != "success":
        typer.secho(f"\n✗ run #{run_id} failed", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"\n✓ run #{run_id} {'(dry-run) ' if dry_run else ''}complete", fg="green")


@app.command()
def apply(
    path: Path | None = PathArg,
    dry_run: bool = typer.Option(False, "--dry-run", help="print commands without executing or writing state"),
    allow_prod: bool = typer.Option(False, "--allow-prod", help="permit changes to tier: prod"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
) -> None:
    """Converge real infrastructure to the IaC declaration."""
    _run(_path(path, _settings()), dry_run=dry_run, allow_prod=allow_prod, yes=yes)


@app.command()
def destroy(
    env: str = typer.Option(..., "--env", help="environment to tear down"),
    path: Path | None = PathArg,
    dry_run: bool = typer.Option(False, "--dry-run"),
    allow_prod: bool = typer.Option(False, "--allow-prod"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Remove every resource of an environment (physical hosts are deregistered, never wiped)."""
    _run(_path(path, _settings()), dry_run=dry_run, allow_prod=allow_prod, yes=yes, destroy_envs=(env,))


@app.command()
def state(env: str | None = typer.Option(None, "--env")) -> None:
    """List recorded resources and their health."""
    settings = _settings()
    with Session(make_engine(settings.db_url)) as s:
        q = select(ResourceState).order_by(ResourceState.address)
        if env:
            q = q.where(ResourceState.env == env)
        rows = list(s.exec(q))
    if not rows:
        typer.echo("(no resources in state)")
    for r in rows:
        status = typer.style(f"{r.status:<9}", fg=STATUS_COLOR.get(r.status, "white"))
        typer.echo(f"{status} {r.address:<50} {r.status_detail[:80]}")


@app.command()
def monitor(once: bool = typer.Option(True, "--once/--loop")) -> None:
    """Run health probes from this machine (one sweep by default)."""
    from labforge.monitor.sweep import Monitor

    settings = _settings()
    m = Monitor(make_engine(settings.db_url), settings)
    if not once:
        asyncio.run(m.run_forever())
        return
    for addr, status in sorted(asyncio.run(m.sweep()).items()):
        typer.echo(typer.style(f"{status:<9}", fg=STATUS_COLOR.get(status, "white")) + f" {addr}")


sim_app = typer.Typer(help="Simulated device lab on localhost (no hardware needed).", no_args_is_help=True)
app.add_typer(sim_app, name="sim")
SIM_CONTROL = "http://127.0.0.1:9100"


@sim_app.command("run")
def sim_run(devices: int = typer.Option(4, help="number of simulated sensors"),
            base_port: int = typer.Option(9100, help="control port; device N uses base+N*10+1 (http) and +2 (BMC)"),
            bmc_credentials: str = typer.Option("admin:admin", envvar="LABFORGE_SIM_BMC_CREDENTIALS")) -> None:
    """Run simulated ARM sensors with Redfish BMCs until interrupted (matches infra/test/sim-lab.yaml)."""
    import time

    from labforge.sim import Simulator, default_devices

    sim = Simulator(default_devices(devices, base_port), base_port, bmc_credentials).start()
    for d in sim.devices.values():
        typer.echo(f"{d.spec.name:<15} device http://127.0.0.1:{d.spec.http_port}  bmc http://127.0.0.1:{d.spec.bmc_port}")
    typer.echo(f"control         http://127.0.0.1:{base_port}/sim   (labforge sim fault <device> crash|hang|overheat|none)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sim.stop()


@sim_app.command("status")
def sim_status(control: str = typer.Option(SIM_CONTROL)) -> None:
    """Show simulated devices: power, firmware, injected fault."""
    import httpx

    for d in httpx.get(f"{control}/sim", timeout=5).json()["devices"]:
        fault = typer.style(d["fault"], fg="red") if d["fault"] else typer.style("none", fg="green")
        typer.echo(f"{d['name']:<15} power {d['power']:<4} firmware {d['firmware']:<8} {d['temperature']:>4.0f}C  fault {fault}")


@sim_app.command("fault")
def sim_fault(device: str, fault: str = typer.Argument(..., help="crash | hang | overheat | none"),
              control: str = typer.Option(SIM_CONTROL)) -> None:
    """Inject (or clear) a fault on a simulated device."""
    import httpx

    r = httpx.post(f"{control}/sim/devices/{device}/fault", json={"fault": fault}, timeout=5)
    if r.status_code != 200:
        typer.secho(r.json().get("error", r.text), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"{device}: fault={r.json()['fault'] or 'none'}")


rig_app = typer.Typer(help="Borrow devices from a rig pool (talks to a running LabForge server).", no_args_is_help=True)
app.add_typer(rig_app, name="rig")


def _rig_request(method: str, server: str, path: str, token: str | None, **kw):
    import httpx

    headers = {**({"X-LabForge-Token": token} if token else {}), **kw.pop("headers", {})}
    r = httpx.request(method, f"{server.rstrip('/')}/api/v1{path}", timeout=120, headers=headers, **kw)
    if r.status_code >= 400:
        detail = r.json().get("detail", r.text) if "json" in r.headers.get("content-type", "") else r.text
        typer.secho(f"error {r.status_code}: {detail}", fg="red", err=True)
        raise typer.Exit(2 if r.status_code == 409 else 1)
    return r.json() if r.content else None


@rig_app.command("list")
def rig_list(server: str = ServerOpt) -> None:
    """Show pools and the state of every device."""
    for pool in _rig_request("GET", server, "/rigs", None):
        typer.echo(f"pool {pool['pool']}: {pool['free']} ready")
        for d in pool["devices"]:
            holder = d["lease"]["holder"] if d["lease"] else ""
            typer.echo(f"  {d['device']:<18} {d['status']:<9} {d['rig']:<12} {holder}")


@rig_app.command("checkout")
def rig_checkout(pool: str = typer.Option(..., "--pool"), holder: str = typer.Option(..., "--holder"),
                 ttl: int = typer.Option(1800, "--ttl", help="lease length in seconds"),
                 output: str = typer.Option("text", "--output", help="text | json | env"),
                 write: Path | None = typer.Option(None, "--write", help="also save the lease as JSON here"),
                 wait: int = typer.Option(0, "--wait", help="seconds to wait in line for a free device"),
                 server: str = ServerOpt, token: str | None = TokenOpt) -> None:
    """Borrow a healthy device. Exit code 2 when none is available (after --wait)."""
    import json
    import time

    import httpx

    started = time.monotonic()
    while True:
        waited = round(time.monotonic() - started, 1)
        r = httpx.post(f"{server.rstrip('/')}/api/v1/rigs/checkout", timeout=120,
                       headers={"X-LabForge-Token": token} if token else {},
                       json={"pool": pool, "holder": holder, "ttl_seconds": ttl,
                             "queued_seconds": waited if wait else None})
        if r.status_code == 409 and time.monotonic() - started < wait:
            typer.echo(f"waiting for a free device in {pool} ({waited:.0f}s)", err=True)
            time.sleep(min(15, max(1, wait / 20)))
            continue
        break
    if r.status_code >= 400:
        typer.secho(f"error {r.status_code}: {r.json().get('detail', r.text)}", fg="red", err=True)
        raise typer.Exit(2 if r.status_code == 409 else 1)
    lease = r.json()
    if write:
        write.write_text(json.dumps(lease, indent=2, default=str))
    if output == "json":
        typer.echo(json.dumps(lease, indent=2, default=str))
    elif output == "env":
        values = {"LABFORGE_LEASE_ID": lease["leaseId"], "LABFORGE_DEVICE": lease["device"],
                  "LABFORGE_DEVICE_HOST": lease["host"], "LABFORGE_DEVICE_HTTP": lease["endpoints"].get("http", ""),
                  "LABFORGE_DEVICE_BMC": lease["endpoints"].get("bmc", "")}
        for k, v in values.items():
            typer.echo(f"{k}={v}")
    else:
        typer.echo(f"lease {lease['leaseId']}: {lease['device']} ({lease['host']}) until {lease['expiresAt']}")
        if lease["note"]:
            typer.echo(f"  {lease['note']}")


def _artifact(path: Path | None) -> tuple[str | None, int | None]:
    if not path:
        return None, None
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


@rig_app.command("flash")
def rig_flash(lease_id: int, version: str = typer.Option(..., "--version", help="firmware version, e.g. git describe"),
              artifact: Path | None = typer.Option(None, "--artifact", exists=True, dir_okay=False,
                                                   help="build output; its sha256 is sent so delivery can be verified"),
              wait: float = typer.Option(60, "--wait", help="seconds to wait for the device to come back healthy"),
              server: str = ServerOpt, token: str | None = TokenOpt) -> None:
    """Install a firmware build on a leased device and wait until it runs healthy."""
    sha256, size = _artifact(artifact)
    r = _rig_request("POST", server, f"/rigs/leases/{lease_id}/flash", token,
                     json={"version": version, "sha256": sha256, "size": size, "wait_seconds": wait})
    typer.echo(f"{r['device']} runs {r['version']} after {r['seconds']}s" + (f" (sha256 {sha256[:12]})" if sha256 else ""))


@rig_app.command("check")
def rig_check(lease_id: int, expect_version: str | None = typer.Option(None, "--expect-version"),
              artifact: Path | None = typer.Option(None, "--artifact", exists=True, dir_okay=False,
                                                   help="expect the device to run exactly this build (sha256)"),
              server: str = ServerOpt) -> None:
    """Verify a leased device is healthy and runs the expected firmware. Exit code 1 if not."""
    r = _rig_request("GET", server, f"/rigs/leases/{lease_id}/device", None)
    running = r["version"] or {}
    problems = [] if r["healthy"] else [f"unhealthy: {r['summary']}"]
    if expect_version and running.get("firmware") != expect_version:
        problems.append(f"runs {running.get('firmware')!r}, expected {expect_version!r}")
    sha256, _ = _artifact(artifact)
    if sha256 and running.get("artifact_sha256") != sha256:
        problems.append(f"artifact sha256 {str(running.get('artifact_sha256'))[:12]} != build {sha256[:12]}")
    typer.echo(f"{r['device']}: firmware {running.get('firmware')} sha256 {str(running.get('artifact_sha256'))[:12]} "
               f"healthy={r['healthy']}")
    for p in problems:
        typer.secho(f"  {p}", fg="red", err=True)
    if problems:
        raise typer.Exit(1)


@rig_app.command("release")
def rig_release(lease_id: int, result: str = typer.Option(..., "--result", help="passed | failed | infra-failed"),
                server: str = ServerOpt, token: str | None = TokenOpt) -> None:
    """Return a device; it is power-cycled back to a known-good state."""
    lease = _rig_request("POST", server, f"/rigs/leases/{lease_id}/release", token, json={"result": result})
    typer.echo(f"lease {lease_id} released ({result}): {lease['note']}")


builds_app = typer.Typer(help="Build-farm speed: ingest BuildPulse runs, see trends and suggestions.",
                         no_args_is_help=True)
app.add_typer(builds_app, name="builds")


@builds_app.command("ingest")
def builds_ingest(paths: list[Path] = typer.Argument(..., exists=True, help="report.json, run dir, or log dir"),
                  runner: str = typer.Option("", "--runner", help="machine or CI runner that built it"),
                  pipeline: str | None = typer.Option(None, "--pipeline", help="override the pipeline name"),
                  queued: float | None = typer.Option(None, "--queued", help="seconds the job waited for a runner"),
                  server: str = ServerOpt, token: str | None = TokenOpt) -> None:
    """Send BuildPulse report.json files to LabForge."""
    import json

    from labforge.builds import report_files

    sent = 0
    for path in paths:
        for f in report_files(path):
            params = {k: v for k, v in {"runner": runner, "pipeline": pipeline, "queued_seconds": queued}.items()
                      if v not in (None, "")}
            r = _rig_request("POST", server, "/builds", token, json=json.loads(f.read_text()), params=params)
            sent += 1
            typer.echo(f"{'+' if r['created'] else '~'} {r['runId']} -> {r['pipeline']}")
    typer.echo(f"{sent} run(s) ingested")


@builds_app.command("stats")
def builds_stats(days: int = typer.Option(30, "--days"), server: str = ServerOpt) -> None:
    """Per-pipeline speed and the suggestions derived from it."""
    data = _rig_request("GET", server, "/builds/stats", None, params={"days": days})
    for p in data["pipelines"]:
        hit = f"{p['compilerCacheHitRate']:.0%}" if p["compilerCacheHitRate"] is not None else "-"
        typer.echo(f"{p['pipeline']:<26} runs {p['runs']:>3}  ok {p['successRate']:.0%}  median {p['medianSeconds']:>6.1f}s"
                   f"  p90 {p['p90Seconds']:>6.1f}s  cache hits {hit}")
    for s in data["suggestions"]:
        typer.echo(f"[{s['severity']}] {s['pipeline']}: {s['text']}")


token_app = typer.Typer(help="API tokens (run on the LabForge server; writes its database directly).",
                        no_args_is_help=True)
app.add_typer(token_app, name="token")


@token_app.command("create")
def token_create(name: str, role: str = typer.Option("viewer", "--role", help="viewer|operator|editor|admin|agent"),
                 envs: str = typer.Option("*", "--envs", help="comma-separated environment globs it may change"),
                 subject: str | None = typer.Option(None, "--subject", help="agent tokens: host address, e.g. dev.host.arm-1")
                 ) -> None:
    """Create a token. It is printed once; only its hash is stored."""
    from labforge.auth import audit, cli_actor, create_token

    engine = make_engine(_settings().db_url)
    try:
        token = create_token(engine, name, role, envs.split(","), subject)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    audit(engine, cli_actor(), "token.create", name, f"role={role} envs={envs}", "cli")
    typer.echo(token)
    typer.secho("Store it now: it cannot be shown again.", fg="yellow", err=True)


@token_app.command("list")
def token_list() -> None:
    from labforge.models import ApiToken

    with Session(make_engine(_settings().db_url)) as s:
        for t in s.exec(select(ApiToken).order_by(ApiToken.name)):
            state = "revoked" if t.revoked_at else (f"used {t.last_used_at:%Y-%m-%d %H:%M}" if t.last_used_at else "never used")
            typer.echo(f"{t.name:<24} {t.prefix}…  {t.role:<9} envs={t.envs}"
                       + (f" subject={t.subject}" if t.subject else "") + f"  {state}")


@token_app.command("revoke")
def token_revoke(name: str) -> None:
    from labforge.auth import audit, cli_actor, revoke_token

    engine = make_engine(_settings().db_url)
    try:
        revoke_token(engine, name)
    except LookupError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    audit(engine, cli_actor(), "token.revoke", name, "", "cli")
    typer.echo(f"{name} revoked")


cra_app = typer.Typer(help="EU CRA traceability: SBOMs per firmware, inventory, vulnerability impact.",
                      no_args_is_help=True)
app.add_typer(cra_app, name="cra")


@cra_app.command("sbom")
def cra_sbom(version: str, sbom: Path = typer.Argument(..., exists=True, dir_okay=False),
             server: str = ServerOpt, token: str | None = TokenOpt) -> None:
    """Upload the SBOM of a firmware version (e.g. BuildPulse's sbom.cdx.json)."""
    import json

    r = _rig_request("POST", server, f"/firmware/{version}/sbom", token, json=json.loads(sbom.read_text()))
    typer.echo(f"{version}: {r['format']} SBOM with {r['components']} components")


@cra_app.command("affected")
def cra_affected(component: str, below: str | None = typer.Option(None, "--below", help="first fixed version"),
                 versions: str | None = typer.Option(None, "--versions", help="comma-separated affected versions"),
                 markdown: bool = typer.Option(False, "--markdown", help="print a report for the CRA notification"),
                 server: str = ServerOpt) -> None:
    """Which firmware contains a vulnerable component, and which devices run it now. Exit 1 if any."""
    import httpx

    params = {k: v for k, v in {"component": component, "below": below, "versions": versions}.items() if v}
    if markdown:
        typer.echo(httpx.get(f"{server.rstrip('/')}/api/v1/cra/affected", params={**params, "format": "markdown"},
                             timeout=30).text)
        return
    r = _rig_request("GET", server, "/cra/affected", None, params=params)
    for f in r["firmware"]:
        typer.echo(f"firmware {f['firmware']}: contains {component} {', '.join(f['componentVersions'])}")
    for d in r["devices"]:
        typer.secho(f"  affected device {d['address']} ({d['env']}) runs {d['version']}", fg="red")
    for d in r["unknown"]:
        typer.secho(f"  no SBOM for {d['address']} running {d['version']}", fg="yellow")
    if not r["firmware"]:
        typer.secho("no firmware with an SBOM contains it", fg="green")
    if r["devices"]:
        raise typer.Exit(1)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web UI + REST API (+ background monitoring)."""
    import uvicorn

    uvicorn.run("labforge.main:create_app", factory=True, host=host, port=port, reload=reload)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def agent(ctx: typer.Context) -> None:
    """Run the host agent (same flags as labforge/agent.py, e.g. --server --env --name --token)."""
    from labforge.agent import main

    raise typer.Exit(main(ctx.args))


if __name__ == "__main__":
    app()
