from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from conftest import ROOT, RecordingRunner, write_env
from labforge import service
from labforge.iac.applier import ApplyContext, ApplyError, apply_plan
from labforge.iac.loader import load_path
from labforge.iac.planner import build_plan
from labforge.iac.providers.loadbalancer import render_haproxy
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import LoadBalancer
from labforge.models import ResourceState

LAB = """apiVersion: labforge/v1
kind: Environment
metadata: {name: lab, tier: TIER}
spec:
  networks:
    - {name: net, cidr: 10.0.0.0/16, subnets: [{name: a, cidr: 10.0.1.0/24}]}
  hosts:
    - name: arm1
      platform: {os: linux, arch: arm64}
      address: ADDR
      subnet: net/a
      bmc: {type: ipmi, address: 10.0.0.5, credentialsRef: bmc}
      power: "on"
      connection: {type: ssh, user: lab, keyRef: key}
      bootstrap: ["echo hi"]
    - name: win1
      platform: {os: windows}
      address: 10.0.1.20
      subnet: net/a
      connection: {type: winrm, user: admin, passwordRef: winpw}
  loadBalancers:
    - name: lb
      driver: haproxy
      host: arm1
      listeners: [{port: 80, protocol: http, targetPort: 8080}]
      targets: [arm1]
      healthCheck: {protocol: http, port: 8080, path: /ok}
  containers:
    - name: db
      host: arm1
      image: postgres:16
      secretEnv: {PGPASSWORD: dbpw}
"""

SECRETS = {"LABFORGE_SECRET_BMC": "root:hunter2", "LABFORGE_SECRET_KEY": "/keys/id", "LABFORGE_SECRET_DBPW": "s3cret",
           "LABFORGE_SECRET_WINPW": "pw"}


def lab(tmp_path: Path, tier: str = "dev", addr: str = "10.0.1.10") -> Path:
    return write_env(tmp_path / "lab.yaml", LAB.replace("TIER", tier).replace("ADDR", addr))


def apply(session, path: Path, *, destroy_envs=()):
    docs = load_path(path)
    plan = build_plan(docs, list(session.exec(select(ResourceState))), destroy_envs)
    runner = RecordingRunner(False, lambda _l: None)
    ctx = ApplyContext(runner=runner, secrets=SecretResolver(environ=SECRETS), log=lambda _l: None,
                       desired=plan.desired,
                       recorded={s.address: s.spec for s in session.exec(select(ResourceState))})
    apply_plan(plan, session, ctx)
    return plan, runner


def test_plan_order_and_lifecycle(tmp_path, session, monkeypatch):
    monkeypatch.setattr("labforge.iac.providers.host.winrm_run", lambda ctx, host, cmd: "")
    path = lab(tmp_path)
    plan, runner = apply(session, path)
    order = [c.address for c in plan.changes]
    assert order.index("lab.network.net") < order.index("lab.host.arm1") < order.index("lab.loadbalancer.lb")
    assert order.index("lab.host.arm1") < order.index("lab.container.db")
    assert plan.counts()["create"] == 5

    # second plan is a no-op
    plan2, runner2 = apply(session, path)
    assert not plan2.has_changes and runner2.calls == []

    # changing the host address updates the host and, via dependency inputs, the LB and container
    plan3, _ = apply(session, lab(tmp_path, addr="10.0.1.11"))
    updated = {c.address for c in plan3.changes if c.action == "update"}
    assert updated == {"lab.host.arm1", "lab.loadbalancer.lb", "lab.container.db"}
    lb = next(c for c in plan3.changes if c.address == "lab.loadbalancer.lb")
    assert lb.diff == ["(dependency inputs)"]

    # destroying deletes dependents first
    plan4, _ = apply(session, path, destroy_envs=("lab",))
    deletes = [c.address for c in plan4.changes if c.action == "delete"]
    assert deletes.index("lab.container.db") < deletes.index("lab.host.arm1") < deletes.index("lab.network.net")
    assert list(session.exec(select(ResourceState))) == []


def test_provider_commands(tmp_path, session, monkeypatch):
    winrm_calls = []
    monkeypatch.setattr("labforge.iac.providers.host.winrm_run", lambda ctx, host, cmd: winrm_calls.append(cmd))
    _, runner = apply(session, lab(tmp_path))
    argvs = [c["argv"] for c in runner.calls]

    ipmi = next(c for c in runner.calls if c["argv"][0] == "ipmitool")
    assert "hunter2" not in " ".join(ipmi["argv"]) and ipmi["env"]["IPMI_PASSWORD"] == "hunter2"
    # power already on (fake ipmitool says so) -> no power-on command
    assert not any(a[0] == "ipmitool" and a[-1] == "on" for a in argvs)

    assert ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-i", "/keys/id", "lab@10.0.1.10", "echo hi"] in argvs

    haproxy = next(c for c in runner.calls if c["input"])
    assert "server arm1 10.0.1.10:8080 check port 8080" in haproxy["input"]

    run = next(c for c in runner.calls if c["argv"][:3] == ["docker", "-H", "ssh://lab@10.0.1.10"] and "run" in c["argv"])
    assert "s3cret" not in " ".join(run["argv"]) and run["env"] == {"PGPASSWORD": "s3cret"}
    assert run["argv"][-1] == "postgres:16"


def test_prod_guard(tmp_path, session):
    docs = load_path(lab(tmp_path, tier="prod"))
    plan = build_plan(docs, [])
    ctx = ApplyContext(runner=RecordingRunner(True, print), secrets=SecretResolver(True), log=print)
    with pytest.raises(ApplyError, match="production"):
        apply_plan(plan, session, ctx)


def test_apply_only_touches_loaded_envs(tmp_path, session, monkeypatch):
    monkeypatch.setattr("labforge.iac.providers.host.winrm_run", lambda ctx, host, cmd: "")
    apply(session, lab(tmp_path))
    plan = build_plan(load_path(ROOT / "examples" / "local"), list(session.exec(select(ResourceState))))
    assert not any(c.action == "delete" for c in plan.changes)


def test_service_dry_run_writes_no_state(engine, settings):
    run_id = service.create_run(engine, "test", dry_run=True)
    run = service.execute_run(engine, settings, run_id, ROOT / "infra", dry_run=True)
    assert run.status == "success", run.log
    assert "[dry-run] $ ipmitool" in run.log and "hunter2" not in run.log
    assert run.summary["applied"]["create"] == 38
    from sqlmodel import Session
    with Session(engine) as s:
        assert list(s.exec(select(ResourceState))) == []


def test_render_haproxy_tcp():
    lb = LoadBalancer(name="x", vip="1.2.3.4", listeners=[{"port": 443, "protocol": "https"}])
    cfg = render_haproxy(lb, [("a", "10.0.0.1")])
    assert "bind 1.2.3.4:443" in cfg and "mode tcp" in cfg and "server a 10.0.0.1:443 check" in cfg
