"""Production release guardrails (milestone 6).

- Gate: a firmware version may ship only when it passed a device test on every required rig pool
  (the latest test of that exact version), and its SBOM is on file (CRA).
- Staged rollout: flash a canary, watch it for a bake period, then roll out batch by batch; the first
  device that does not come back healthy, or turns unhealthy while baking, rolls every flashed device
  back to the firmware it had before.
- Production changes: an apply that changes a `tier: prod` environment needs a change request for
  exactly that plan, approved by people other than the requester, applied inside a change window.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from labforge.iac.loader import load_kind
from labforge.iac.planner import Plan
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import ReleasePolicyDoc
from labforge.models import ChangeRequest, Lease, ResourceState, Rollout, Sbom, utcnow

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ReleaseError(Exception):
    def __init__(self, message: str, status: int = 409, **detail):
        self.status = status
        self.detail = {"error": message, **detail}
        super().__init__(message)


def policy(workspace_root) -> ReleasePolicyDoc | None:
    docs = load_kind(workspace_root, ReleasePolicyDoc)
    return docs[0] if docs else None


# --- gate ---------------------------------------------------------------------------


def gate(session: Session, version: str, pol: ReleasePolicyDoc | None) -> dict:
    checks = []
    for pool in (pol.spec.gate.required_pools if pol else []):
        last = session.exec(select(Lease).where(Lease.pool == pool, Lease.firmware_version == version,
                                                Lease.status == "released").order_by(Lease.released_at.desc())).first()
        if last is None:
            checks.append({"check": f"device test on {pool}", "ok": False, "detail": "not tested on this pool yet"})
        else:
            checks.append({"check": f"device test on {pool}", "ok": last.result == "passed",
                           "detail": f"lease {last.id} by {last.holder}: {last.result}"})
    if pol is None or pol.spec.gate.require_sbom:
        has = session.exec(select(Sbom).where(Sbom.version == version)).first() is not None
        checks.append({"check": "SBOM on file", "ok": has, "detail": "uploaded" if has else "missing (labforge cra sbom)"})
    return {"version": version, "ok": all(c["ok"] for c in checks), "checks": checks}


# --- staged rollout -------------------------------------------------------------------


def targets(session: Session, env: str, selector: dict[str, str]) -> tuple[list[ResourceState], list[str]]:
    hosts = session.exec(select(ResourceState).where(ResourceState.env == env, ResourceState.kind == "host")
                         .order_by(ResourceState.name))
    chosen, skipped = [], []
    for h in hosts:
        labels = h.spec.get("labels", {})
        if any(labels.get(k) != v for k, v in selector.items()):
            continue
        if session.exec(select(Lease).where(Lease.address == h.address, Lease.status == "active")).first():
            skipped.append(f"{h.name} (checked out to a test)")
            continue
        chosen.append(h)
    return chosen, skipped


def batches(addresses: list[str], canary: int, batch_size: int) -> list[list[str]]:
    out = [addresses[:canary]] if addresses else []
    rest = addresses[canary:]
    out += [rest[i:i + batch_size] for i in range(0, len(rest), batch_size)]
    return [b for b in out if b]


def start_rollout(engine: Engine, pol: ReleasePolicyDoc | None, version: str, *, env: str | None = None,
                  selector: dict[str, str] | None = None, canary: int | None = None, batch_size: int | None = None,
                  bake_seconds: int | None = None, sha256: str | None = None, by: str = "", force: bool = False,
                  secrets: SecretResolver | None = None, background: bool = True, flash_wait: float = 120.0) -> int:
    d = pol.spec.rollout if pol else None
    env = env or (d.env if d else None)
    if not env:
        raise ReleaseError("no target environment (pass env, or set rollout.env in the ReleasePolicy)", 400)
    selector = selector if selector is not None else (d.selector if d else {})
    with Session(engine) as session:
        result = gate(session, version, pol)
        if not result["ok"] and not force:
            raise ReleaseError(f"release gate failed for {version}", 409, gate=result)
        hosts, skipped = targets(session, env, selector)
        if not hosts:
            raise ReleaseError(f"no hosts in {env} match {selector}", 400, skipped=skipped)
        plan = batches([h.address for h in hosts], canary or (d.canary if d else 1), batch_size or (d.batch_size if d else 2))
        rollout = Rollout(version=version, sha256=sha256, env=env, selector_json=json.dumps(selector),
                          batches_json=json.dumps(plan), created_by=by,
                          bake_seconds=bake_seconds if bake_seconds is not None else (d.bake_seconds if d else 60),
                          log=(f"gate: {'passed' if result['ok'] else 'FORCED past a failed gate'}\n"
                               + "".join(f"skipped {s}\n" for s in skipped)))
        session.add(rollout)
        session.commit()
        session.refresh(rollout)
        rollout_id = rollout.id
    assert rollout_id is not None
    if background:
        threading.Thread(target=run_rollout, args=(engine, rollout_id, secrets, flash_wait), daemon=True,
                         name=f"rollout-{rollout_id}").start()
    else:
        run_rollout(engine, rollout_id, secrets, flash_wait)
    return rollout_id


def abort(engine: Engine, rollout_id: int) -> None:
    with Session(engine) as s:
        r = s.get(Rollout, rollout_id)
        if not r or r.status != "running":
            raise ReleaseError(f"rollout {rollout_id} is not running")
        r.status = "aborting"
        s.add(r)
        s.commit()


def run_rollout(engine: Engine, rollout_id: int, secrets: SecretResolver | None = None,
                flash_wait: float = 120.0, poll: float = 1.0) -> None:
    from labforge.rigs import check, install_and_wait, running_version

    secrets = secrets or SecretResolver()

    def log(line: str) -> None:
        with Session(engine) as s:
            r = s.get(Rollout, rollout_id)
            r.log += line + "\n"
            s.add(r)
            s.commit()

    def load() -> Rollout:
        with Session(engine) as s:
            return s.get(Rollout, rollout_id)

    def save(**fields) -> None:
        with Session(engine) as s:
            r = s.get(Rollout, rollout_id)
            for k, v in fields.items():
                setattr(r, k, v)
            s.add(r)
            s.commit()

    def state_of(address: str) -> ResourceState:
        with Session(engine) as s:
            return s.exec(select(ResourceState).where(ResourceState.address == address)).one()

    r = load()
    plan: list[list[str]] = json.loads(r.batches_json)
    previous: dict[str, str] = json.loads(r.previous_json)
    flashed: list[str] = []

    def rollback(reason: str, final: str) -> None:
        log(f"ROLLBACK: {reason}")
        for address in reversed(flashed):
            before = previous.get(address)
            if not before:
                log(f"  {address}: previous firmware unknown, left as is")
                continue
            try:
                ok, _, _ = install_and_wait(state_of(address), before, None, None, secrets, flash_wait)
            except Exception as e:  # keep rolling the others back
                log(f"  {address}: rollback to {before} FAILED: {e}")
                continue
            log(f"  {address}: back on {before}" + ("" if ok else " - NOT healthy, needs a person"))
        save(status=final, finished_at=utcnow())

    for index, batch in enumerate(plan):
        if load().status == "aborting":
            rollback("aborted by an operator", "aborted")
            return
        save(current_batch=index)
        name = "canary" if index == 0 else f"batch {index}"
        log(f"{name}: {', '.join(a.rsplit('.', 1)[-1] for a in batch)}")
        for address in batch:
            state = state_of(address)
            previous[address] = running_version(state) or previous.get(address) or ""
            save(previous_json=json.dumps(previous))
            flashed.append(address)
            try:
                ok, running, health = install_and_wait(state, r.version, r.sha256, None, secrets, flash_wait)
            except Exception as e:
                rollback(f"flashing {state.name} failed: {e}", "rolled_back")
                return
            if not ok:
                rollback(f"{state.name} did not come back healthy on {r.version}"
                         + (f" ({health.summary})" if health else ""), "rolled_back")
                return
            log(f"  {state.name}: runs {r.version} (was {previous[address] or 'unknown'})")
        deadline = time.monotonic() + r.bake_seconds
        log(f"  baking {r.bake_seconds}s")
        while time.monotonic() < deadline:
            if load().status == "aborting":
                rollback("aborted by an operator", "aborted")
                return
            for address in flashed:
                state = state_of(address)
                health = check(state, secrets)
                if not health.healthy:
                    rollback(f"{state.name} became unhealthy while baking: {health.summary}", "rolled_back")
                    return
            time.sleep(poll)
    log(f"done: {len(flashed)} device(s) on {r.version}")
    save(status="succeeded", finished_at=utcnow(), current_batch=len(plan))


# --- production change requests -----------------------------------------------------------


def prod_envs(plan: Plan) -> list[str]:
    return sorted({c.env for c in plan.changes if c.tier == "prod" and c.action != "noop"})


def plan_hash(plan: Plan) -> str:
    changes = sorted((c.action, c.address, c.hash or "") for c in plan.changes if c.action != "noop")
    return hashlib.sha256(json.dumps(changes).encode()).hexdigest()[:16]


def request_change(engine: Engine, plan: Plan, path: str, reason: str, by: str) -> ChangeRequest:
    if not plan.has_changes:
        raise ReleaseError("nothing to change", 400)
    with Session(engine) as s:
        cr = ChangeRequest(path=path, envs=",".join(prod_envs(plan) or plan.envs), plan_hash=plan_hash(plan),
                           summary_json=json.dumps({"counts": plan.counts(), "changes": [
                               f"{c.action} {c.address}" for c in plan.changes if c.action != "noop"][:200]}),
                           reason=reason, requested_by=by)
        s.add(cr)
        s.commit()
        s.refresh(cr)
        return cr


def decide(engine: Engine, change_id: int, by: str, approve: bool, pol: ReleasePolicyDoc | None) -> ChangeRequest:
    with Session(engine) as s:
        cr = s.get(ChangeRequest, change_id)
        if not cr:
            raise ReleaseError(f"change request {change_id} not found", 404)
        if cr.status not in ("pending", "approved"):
            raise ReleaseError(f"change request {change_id} is {cr.status}")
        if by == cr.requested_by:
            raise ReleaseError("the requester cannot approve or reject their own change", 403)
        if not approve:
            cr.status, cr.decided_at = "rejected", utcnow()
        else:
            approvals = json.loads(cr.approvals_json)
            if by not in approvals:
                approvals.append(by)
            cr.approvals_json = json.dumps(approvals)
            if len(approvals) >= (pol.spec.approvals if pol else 1):
                cr.status, cr.decided_at = "approved", utcnow()
        s.add(cr)
        s.commit()
        s.refresh(cr)
        return cr


def in_window(pol: ReleasePolicyDoc | None, now: datetime) -> bool:
    windows = pol.spec.change_windows if pol else []
    if not windows:
        return True
    day, hm = DAYS[now.weekday()], now.strftime("%H:%M")
    return any(day in w.days and w.start <= hm < w.end for w in windows)


def check_production(engine: Engine, plan: Plan, path: str, change_id: int | None, pol: ReleasePolicyDoc | None,
                     now: datetime | None = None) -> ChangeRequest | None:
    """Raise unless a production change is covered by an approved change request, inside a window."""
    envs = prod_envs(plan)
    if not envs:
        return None
    if change_id is None:
        raise ReleaseError(f"changes production ({', '.join(envs)}): needs an approved change request "
                           "(labforge change request), applied with --change-request <id>", 403)
    with Session(engine) as s:
        cr = s.get(ChangeRequest, change_id)
    if not cr or cr.status != "approved":
        raise ReleaseError(f"change request {change_id} is {cr.status if cr else 'missing'}, not approved", 403)
    if cr.path != path:
        raise ReleaseError(f"change request {change_id} was for {cr.path or 'the workspace'}, not {path or 'the workspace'}", 403)
    if cr.plan_hash != plan_hash(plan):
        raise ReleaseError(f"the plan changed since change request {change_id} was approved; request it again", 409)
    if not in_window(pol, now or utcnow()):
        windows = "; ".join(f"{','.join(w.days)} {w.start}-{w.end} UTC" for w in pol.spec.change_windows)
        raise ReleaseError(f"outside the production change windows ({windows})", 403)
    return cr


def mark_applied(engine: Engine, change_id: int) -> None:
    with Session(engine) as s:
        cr = s.get(ChangeRequest, change_id)
        if cr:
            cr.status = "applied"
            s.add(cr)
            s.commit()
