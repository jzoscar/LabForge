"""Operations shared by the CLI, the REST API and the web UI."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from labforge.config import Settings
from labforge.iac.applier import ApplyContext, apply_plan, guard_prod
from labforge.iac.loader import load_path
from labforge.iac.planner import Plan, build_plan
from labforge.iac.runner import Runner
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import EnvironmentDoc
from labforge.models import AgentReport, ResourceState, Run, utcnow

_apply_lock = threading.Lock()

RunnerFactory = Callable[[bool, Callable[[str], None]], Runner]


class BusyError(Exception):
    pass


def make_plan(session: Session, path: Path, destroy_envs: tuple[str, ...] = ()) -> tuple[list[EnvironmentDoc], Plan]:
    docs = load_path(path)
    return docs, build_plan(docs, list(session.exec(select(ResourceState))), destroy_envs)


def create_run(engine: Engine, source: str, dry_run: bool, action: str = "apply") -> int:
    with Session(engine) as s:
        run = Run(action=action, source=source, dry_run=dry_run)
        s.add(run)
        s.commit()
        assert run.id is not None
        return run.id


def execute_run(
    engine: Engine,
    settings: Settings,
    run_id: int,
    path: Path,
    *,
    dry_run: bool,
    allow_prod: bool = False,
    destroy_envs: tuple[str, ...] = (),
    runner_factory: RunnerFactory | None = None,
    echo: Callable[[str], None] | None = None,
    change_request: int | None = None,
) -> Run:
    """Plan + apply, streaming log lines into the Run row. Never raises for apply failures."""

    def log(line: str) -> None:
        if echo:
            echo(line)
        with Session(engine) as s:
            run = s.get(Run, run_id)
            assert run
            run.log += line + "\n"
            s.add(run)
            s.commit()

    from labforge import locks

    if not _apply_lock.acquire(blocking=False):
        _finish(engine, run_id, "failed", {"error": "another apply is running"})
        log("error: another apply is running")
        raise BusyError("another apply is running")
    if not locks.acquire(engine, "apply", 3600):  # another LabForge instance is applying
        _apply_lock.release()
        who = locks.holder(engine, "apply") or "another instance"
        _finish(engine, run_id, "failed", {"error": f"another apply is running on {who}"})
        log(f"error: another apply is running on {who}")
        raise BusyError(f"another apply is running on {who}")
    try:
        with Session(engine) as session:
            try:
                docs, plan = make_plan(session, path, destroy_envs)
                approved = _production_gate(engine, settings, plan, path, dry_run, change_request)
                prod_ok = allow_prod or dry_run or approved is not None
                guard_prod(plan, prod_ok)
                if approved is not None:
                    log(f"production change request #{approved.id} approved by "
                        f"{', '.join(json.loads(approved.approvals_json))}")
                states = {s.address: s.spec for s in session.exec(select(ResourceState))}
                runner = (runner_factory or Runner)(dry_run, log)
                ctx = ApplyContext(
                    runner=runner,
                    secrets=SecretResolver(dry_run=dry_run),
                    log=log,
                    base_dir=path if path.is_dir() else path.parent,
                    workdir=settings.data_dir,
                    desired=plan.desired,
                    recorded=states,
                    env_dirs={d.metadata.name: Path(d._source).parent for d in docs if d._source},
                )
                log(f"plan: {plan.counts()} for env(s) {', '.join(plan.envs) or '-'}" + (" [dry-run]" if dry_run else ""))
                done = apply_plan(plan, session, ctx, allow_prod=prod_ok)
                log("apply complete" + (" (dry-run, no state written)" if dry_run else ""))
                _finish(engine, run_id, "success", {"planned": plan.counts(), "applied": done})
                if approved is not None and not dry_run:
                    from labforge.release import mark_applied

                    mark_applied(engine, approved.id)
            except Exception as e:
                log(f"error: {e}")
                _finish(engine, run_id, "failed", {"error": str(e)})
    finally:
        try:
            locks.release(engine, "apply")
        except Exception:  # the lease expires on its own; never leak the in-process lock
            logging.getLogger("labforge").exception("releasing the apply lock failed")
        finally:
            _apply_lock.release()
    with Session(engine) as s:
        run = s.get(Run, run_id)
        assert run
        return run


def workspace_relative(settings: Settings, path: Path) -> str:
    root = settings.workspace().root
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)
    return "" if rel == "." else rel


def _production_gate(engine: Engine, settings: Settings, plan, path: Path, dry_run: bool, change_request: int | None):
    """Production changes need an approved change request when a ReleasePolicy exists (or one is given)."""
    from labforge import release

    if dry_run or not release.prod_envs(plan):
        return None
    pol = release.policy(settings.workspace().root)
    if pol is None and change_request is None:
        return None  # no release policy: the plain --allow-prod guard applies
    return release.check_production(engine, plan, workspace_relative(settings, path), change_request, pol)


def start_run_background(engine: Engine, settings: Settings, path: Path, *, dry_run: bool,
                         allow_prod: bool, source: str, change_request: int | None = None) -> int:
    run_id = create_run(engine, source, dry_run)

    def target() -> None:
        try:
            execute_run(engine, settings, run_id, path, dry_run=dry_run, allow_prod=allow_prod,
                        change_request=change_request)
        except BusyError:
            pass

    threading.Thread(target=target, name=f"labforge-run-{run_id}", daemon=True).start()
    return run_id


def _finish(engine: Engine, run_id: int, status: str, summary: dict) -> None:
    with Session(engine) as s:
        run = s.get(Run, run_id)
        assert run
        run.status, run.summary_json, run.finished_at = status, json.dumps(summary), utcnow()
        s.add(run)
        s.commit()


def ingest_agent_report(session: Session, payload: dict) -> tuple[AgentReport, bool]:
    addr = f"{payload['env']}.host.{payload['name']}"
    managed = session.exec(select(ResourceState).where(ResourceState.address == addr)).first() is not None
    report = AgentReport(
        address=addr,
        os=payload.get("os"),
        arch=payload.get("arch"),
        cpu_percent=payload["cpu_percent"],
        mem_percent=payload["mem_percent"],
        disk_percent=payload.get("disk_percent"),
        temp_c=payload.get("temp_c"),
        payload_json=json.dumps({k: payload.get(k) for k in ("hostname", "extra")}, default=str),
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report, managed
