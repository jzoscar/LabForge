"""Execute a plan through providers and record resulting state."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from labforge.iac.planner import Change, DesiredResource, Plan, address
from labforge.iac.providers import PROVIDERS, ProviderError
from labforge.iac.runner import Runner
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Host
from labforge.models import ResourceState, utcnow


class ApplyError(Exception):
    pass


@dataclass
class ApplyContext:
    runner: Runner
    secrets: SecretResolver
    log: Callable[[str], None]
    base_dir: Path = Path(".")
    workdir: Path = Path(".labforge")
    desired: dict[str, DesiredResource] = field(default_factory=dict)
    recorded: dict[str, dict] = field(default_factory=dict)  # address -> spec from state

    @property
    def dry_run(self) -> bool:
        return self.runner.dry_run

    def host(self, env: str, name: str) -> Host:
        addr = address(env, "host", name)
        spec = self.desired[addr].spec if addr in self.desired else self.recorded.get(addr)
        if spec is None:
            raise ProviderError(f"unknown host {addr}")
        return Host.model_validate(spec)


def guard_prod(plan: Plan, allow_prod: bool) -> None:
    touched = sorted({c.env for c in plan.changes if c.tier == "prod" and c.action != "noop"})
    if touched and not allow_prod:
        raise ApplyError(f"plan changes production environment(s) {', '.join(touched)}; pass allow_prod to proceed")


def _record(session: Session, c: Change, outputs: dict) -> None:
    st = session.exec(select(ResourceState).where(ResourceState.address == c.address)).first()
    if st is None:
        st = ResourceState(address=c.address, env=c.env, tier=c.tier, kind=c.kind, name=c.name,
                           spec_json="{}", spec_hash="")
    st.tier = c.tier
    st.spec_json = json.dumps(c.spec, sort_keys=True)
    st.spec_hash = c.hash or ""
    st.outputs_json = json.dumps(outputs, sort_keys=True, default=str)
    st.updated_at = utcnow()
    session.add(st)
    session.commit()


def apply_plan(plan: Plan, session: Session, ctx: ApplyContext, allow_prod: bool = False) -> dict[str, int]:
    """Apply changes in plan order; stops at the first failure (earlier changes stay recorded)."""
    guard_prod(plan, allow_prod)
    done = {"create": 0, "update": 0, "delete": 0}
    for c in plan.changes:
        if c.action == "noop":
            continue
        ctx.log(f"==> {c.action} {c.address}" + (f"  ({', '.join(c.diff)})" if c.diff else ""))
        provider = PROVIDERS[c.kind]
        if c.action == "delete":
            provider.destroy(c, ctx)
            if not ctx.dry_run:
                st = session.exec(select(ResourceState).where(ResourceState.address == c.address)).first()
                if st:
                    session.delete(st)
                    session.commit()
        else:
            outputs = provider.apply(c, ctx) or {}
            if not ctx.dry_run:
                _record(session, c, outputs)
        done[c.action] += 1
    return done
