"""Release guardrails against the simulated lab: gate, staged rollout and rollback, change requests."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from labforge import release, rigs, service
from labforge.auth import create_token
from labforge.cra import store_sbom
from labforge.iac.spec import ReleasePolicyDoc
from labforge.main import create_app
from labforge.models import ChangeRequest, Rollout
from test_rigs import lab  # noqa: F401  (simulated lab fixture)

CDX = {"bomFormat": "CycloneDX", "components": [{"name": "zlib", "version": "1.3.1"}]}


def policy(**spec) -> ReleasePolicyDoc:
    base = {"gate": {"requiredPools": ["sim-sensor"]},
            "rollout": {"env": "sim-lab", "selector": {"role": "sensor"}, "canary": 1, "batchSize": 2, "bakeSeconds": 1}}
    return ReleasePolicyDoc.model_validate({"apiVersion": "labforge/v1", "kind": "ReleasePolicy",
                                            "metadata": {"name": "p"}, "spec": {**base, **spec}})


def device_test(engine, version: str, result: str = "passed") -> None:
    lease = rigs.checkout(engine, "sim-sensor", "ci")
    rigs.flash(engine, lease["leaseId"], version, wait_seconds=10)
    rigs.release(engine, lease["leaseId"], result)


def test_gate_needs_a_passed_device_test_and_an_sbom(lab, engine):  # noqa: F811
    pol = policy()
    with Session(engine) as s:
        g = release.gate(s, "2.0.0", pol)
        assert not g["ok"] and [c["ok"] for c in g["checks"]] == [False, False]
    device_test(engine, "2.0.0", "failed")
    with Session(engine) as s:
        store_sbom(s, "2.0.0", CDX)
        assert "failed" in release.gate(s, "2.0.0", pol)["checks"][0]["detail"]
    device_test(engine, "2.0.0", "passed")  # the latest test of the version counts
    with Session(engine) as s:
        assert release.gate(s, "2.0.0", pol)["ok"]


def test_staged_rollout_reaches_every_station(lab, engine):  # noqa: F811
    rid = release.start_rollout(engine, policy(), "2.1.0", force=True, background=False)
    with Session(engine) as s:
        r = s.get(Rollout, rid)
    assert r.status == "succeeded", r.log
    assert json.loads(r.batches_json) == [["sim-lab.host.sim-sensor-01"],
                                          ["sim-lab.host.sim-sensor-02", "sim-lab.host.sim-sensor-03"],
                                          ["sim-lab.host.sim-sensor-04"]]
    assert all(d.firmware == "2.1.0" for d in lab.devices.values())
    assert "FORCED past a failed gate" in r.log and "canary: sim-sensor-01" in r.log


def test_bad_build_is_rolled_back_at_the_canary(lab, engine):  # noqa: F811
    rid = release.start_rollout(engine, policy(), "2.2.0-broken", force=True, background=False, flash_wait=3)
    with Session(engine) as s:
        r = s.get(Rollout, rid)
    assert r.status == "rolled_back"
    assert "sim-sensor-01 did not come back healthy on 2.2.0-broken" in r.log
    assert "sim-sensor-01: back on 1.4.2" in r.log
    assert [d.firmware for d in lab.devices.values()] == ["1.4.2"] * 4  # nothing else was touched


def test_unhealthy_while_baking_rolls_everything_back(lab, engine):  # noqa: F811
    pol = policy(rollout={"env": "sim-lab", "selector": {"role": "sensor"}, "canary": 1, "batchSize": 3,
                          "bakeSeconds": 8})
    rid = release.start_rollout(engine, pol, "2.3.0", force=True, background=True, flash_wait=3)
    for _ in range(200):
        with Session(engine) as s:
            if "baking" in s.get(Rollout, rid).log:
                break
        time.sleep(0.05)
    lab.inject("sim-sensor-01", "overheat")  # the canary degrades while being watched
    for _ in range(300):
        with Session(engine) as s:
            r = s.get(Rollout, rid)
        if r.status not in ("running", "aborting"):
            break
        time.sleep(0.1)
    assert r.status == "rolled_back" and "became unhealthy while baking" in r.log
    assert lab.devices["sim-sensor-01"].firmware == "1.4.2" and lab.devices["sim-sensor-02"].firmware == "1.4.2"


def test_rollout_refuses_a_failed_gate(lab, engine):  # noqa: F811
    with pytest.raises(release.ReleaseError, match="release gate failed"):
        release.start_rollout(engine, policy(), "9.9.9", background=False)


def test_change_windows():
    pol = policy(changeWindows=[{"days": ["tue", "wed"], "start": "09:00", "end": "16:00"}])
    assert release.in_window(pol, datetime(2026, 11, 10, 10, 0))  # Tuesday
    assert not release.in_window(pol, datetime(2026, 11, 10, 17, 0))
    assert not release.in_window(pol, datetime(2026, 11, 9, 10, 0))  # Monday
    assert release.in_window(policy(), datetime(2026, 11, 9, 3, 0))  # no windows: any time


PROD = """apiVersion: labforge/v1
kind: Environment
metadata: {name: line-1, tier: prod}
spec:
  networks:
    - {name: line-net, cidr: 10.90.0.0/24}
"""
POLICY = """apiVersion: labforge/v1
kind: ReleasePolicy
metadata: {name: rp}
spec:
  approvals: 1
"""


def test_production_needs_an_approved_change_request(settings, tmp_path):
    ws = tmp_path / "iac"
    ws.mkdir()
    (ws / "prod.yaml").write_text(PROD)
    (ws / "release.yaml").write_text(POLICY)
    settings.iac_path = ws
    app = create_app(settings)
    engine = app.state.engine

    run = service.execute_run(engine, settings, service.create_run(engine, "t", False), ws, dry_run=False)
    assert run.status == "failed" and "needs an approved change request" in run.summary["error"]
    assert service.execute_run(engine, settings, service.create_run(engine, "t", True), ws,
                               dry_run=True).status == "success"  # planning production is fine

    alice = {"X-LabForge-Token": create_token(engine, "alice", "admin")}
    bob = {"X-LabForge-Token": create_token(engine, "bob", "admin")}
    with TestClient(app) as c:
        cr = c.post("/api/v1/changes", json={"reason": "CHG-42 new line"}, headers=alice).json()
        assert cr["status"] == "pending" and cr["envs"] == "line-1"
        r = c.post(f"/api/v1/changes/{cr['id']}/approve", headers=alice)
        assert r.status_code == 403 and "own change" in r.json()["detail"]["error"]
        assert c.post(f"/api/v1/changes/{cr['id']}/approve", headers=bob).json()["status"] == "approved"
        assert "CHG-42" in c.get("/releases").text

    run = service.execute_run(engine, settings, service.create_run(engine, "t", False), ws, dry_run=False,
                              change_request=cr["id"])
    assert run.status == "success", run.log
    assert "approved by token:bob" in run.log
    with Session(engine) as s:
        assert s.get(ChangeRequest, cr["id"]).status == "applied"

    (ws / "prod.yaml").write_text(PROD.replace("10.90.0.0/24", "10.91.0.0/24"))
    with Session(engine) as s:
        _, plan = service.make_plan(s, ws)
    stale = release.request_change(engine, plan, "", "x", "carol")
    (ws / "prod.yaml").write_text(PROD.replace("10.90.0.0/24", "10.92.0.0/24"))  # edited after the request
    release.decide(engine, stale.id, "dave", True, None)
    run = service.execute_run(engine, settings, service.create_run(engine, "t", False), ws, dry_run=False,
                              change_request=stale.id)
    assert run.status == "failed" and "plan changed since change request" in run.summary["error"]


def test_release_api_and_page(lab, settings, engine):  # noqa: F811
    with TestClient(create_app(settings)) as c:
        g = c.get("/api/v1/releases/1.0.0/gate").json()
        assert g["ok"] is False and g["checks"][-1]["check"] == "SBOM on file"
        rid = c.post("/api/v1/releases/3.0.0/rollout",
                     json={"env": "sim-lab", "selector": {"role": "sensor"}, "bake_seconds": 0, "force": True}).json()["rolloutId"]
        for _ in range(300):
            r = c.get(f"/api/v1/rollouts/{rid}").json()
            if r["status"] != "running":
                break
            time.sleep(0.1)
        assert r["status"] == "succeeded", r["log"]
        page = c.get("/releases", params={"version": "3.0.0"}).text
        assert "Staged rollout" in page and "succeeded" in page and "blocked" in page
