"""Diff desired IaC state against recorded state into an ordered plan."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from graphlib import TopologicalSorter

from labforge.iac.spec import KIND_MODELS, EnvironmentDoc, Model
from labforge.models import ResourceState

# Deletes run dependents-first.
DELETE_RANK = {"container": 0, "loadbalancer": 1, "host": 2, "network": 3}


def address(env: str, kind: str, name: str) -> str:
    return f"{env}.{kind}.{name}"


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


@dataclass
class DesiredResource:
    address: str
    env: str
    tier: str
    kind: str
    name: str
    spec: dict
    depends_on: list[str]
    # Facts resolved from dependencies (e.g. target host addresses) that the
    # provider renders into the real resource; changing them forces an update.
    inputs: dict = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return spec_hash({"spec": self.spec, "inputs": self.inputs}) if self.inputs else spec_hash(self.spec)


def _dump(model: Model) -> dict:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def flatten(doc: EnvironmentDoc) -> list[DesiredResource]:
    env, tier, s = doc.metadata.name, doc.metadata.tier, doc.spec
    out: list[DesiredResource] = []

    hosts = {h.name: h for h in s.hosts}

    def endpoint(name: str) -> dict:
        h = hosts[name]
        return {"address": h.address, "connection": h.connection.type, "user": h.connection.user, "port": h.connection.port}

    def add(kind: str, model: Model, deps: list[str], inputs: dict | None = None) -> None:
        out.append(DesiredResource(address(env, kind, model.name), env, tier, kind, model.name, _dump(model), deps,
                                   inputs or {}))

    for n in s.networks:
        add("network", n, [])
    for h in s.hosts:
        add("host", h, [address(env, "network", h.subnet.split("/")[0])] if h.subnet else [])
    for lb in s.load_balancers:
        deps = [address(env, "host", t) for t in lb.targets]
        if lb.network:
            deps.append(address(env, "network", lb.network))
        if lb.host:
            deps.append(address(env, "host", lb.host))
        inputs = {"targets": {t: hosts[t].address for t in lb.targets}}
        if lb.host:
            inputs["host"] = endpoint(lb.host)
        add("loadbalancer", lb, deps, inputs)
    for c in s.containers:
        add("container", c, [address(env, "host", c.host)], {"host": endpoint(c.host)})
    return out


def diff_paths(old, new, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for k in sorted(old.keys() | new.keys()):
            out += diff_paths(old.get(k), new.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    return [] if old == new else [prefix or "(root)"]


@dataclass
class Change:
    action: str  # create | update | delete | noop
    address: str
    env: str
    tier: str
    kind: str
    name: str
    spec: dict | None = None
    old_spec: dict | None = None
    diff: list[str] = field(default_factory=list)
    hash: str | None = None  # desired hash recorded in state after apply

    def model(self) -> Model:
        return KIND_MODELS[self.kind].model_validate(self.spec if self.spec is not None else self.old_spec)


@dataclass
class Plan:
    changes: list[Change]
    envs: list[str]
    desired: dict[str, DesiredResource]

    def counts(self) -> dict[str, int]:
        c = Counter(ch.action for ch in self.changes)
        return {a: c.get(a, 0) for a in ("create", "update", "delete", "noop")}

    @property
    def has_changes(self) -> bool:
        return any(ch.action != "noop" for ch in self.changes)


def build_plan(
    docs: list[EnvironmentDoc],
    states: list[ResourceState],
    destroy_envs: tuple[str, ...] | list[str] = (),
) -> Plan:
    """Plan for the given environments. Only state belonging to those environments
    (plus ``destroy_envs``) is considered, so applying one env never touches another."""
    docs = [d for d in docs if d.metadata.name not in destroy_envs]
    desired: dict[str, DesiredResource] = {}
    for d in docs:
        for r in flatten(d):
            desired[r.address] = r
    scope = {d.metadata.name for d in docs} | set(destroy_envs)
    current = {s.address: s for s in states if s.env in scope}

    changes: list[Change] = []
    for addr in sorted(current.keys() - desired.keys(), key=lambda a: (DELETE_RANK.get(current[a].kind, 9), a)):
        st = current[addr]
        changes.append(Change("delete", addr, st.env, st.tier, st.kind, st.name, old_spec=st.spec))

    graph = {a: [d for d in r.depends_on if d in desired] for a, r in desired.items()}
    for addr in TopologicalSorter(graph).static_order():
        r = desired[addr]
        st = current.get(addr)
        if st is None:
            changes.append(Change("create", addr, r.env, r.tier, r.kind, r.name, spec=r.spec, hash=r.hash))
        elif st.spec_hash != r.hash:
            changes.append(
                Change("update", addr, r.env, r.tier, r.kind, r.name, spec=r.spec, old_spec=st.spec,
                       diff=diff_paths(st.spec, r.spec) or ["(dependency inputs)"], hash=r.hash)
            )
        else:
            changes.append(Change("noop", addr, r.env, r.tier, r.kind, r.name, spec=r.spec, old_spec=st.spec,
                                  hash=r.hash))
    return Plan(changes=changes, envs=sorted(scope), desired=desired)
