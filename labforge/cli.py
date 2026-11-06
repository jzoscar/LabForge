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
    p = _path(path, _settings())
    docs = _load_or_exit(p)
    for d in docs:
        s = d.spec
        typer.secho(f"✓ {d.metadata.name} [{d.metadata.tier}]: {len(s.networks)} networks, "
                    f"{len(s.load_balancers)} LBs, {len(s.hosts)} hosts, {len(s.containers)} containers", fg="green")


@app.command()
def plan(path: Path | None = PathArg, all: bool = typer.Option(False, "--all", help="also show unchanged")) -> None:
    """Show what apply would change."""
    from labforge.iac.planner import build_plan

    settings = _settings()
    docs = _load_or_exit(_path(path, settings))
    with Session(make_engine(settings.db_url)) as s:
        _print_plan(build_plan(docs, list(s.exec(select(ResourceState)))), show_noop=all)


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

    action = "destroy" if destroy_envs else "apply"
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
