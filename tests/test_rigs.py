"""End to end against the simulated lab: IaC apply over Redfish, monitoring, rig checkout."""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import ROOT
from labforge import rigs, service
from labforge.main import create_app
from labforge.models import DeviceHealth, Lease, utcnow
from labforge.monitor.sweep import Monitor
from labforge.sim import DeviceSpec, Simulator


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def lab(tmp_path, monkeypatch, settings, engine):
    """Simulator on free ports + the sim-lab IaC rewritten to those ports, applied for real."""
    monkeypatch.setenv("LABFORGE_SECRET_SIM_BMC", "admin:admin")
    specs = [DeviceSpec(f"sim-sensor-0{i}", _free_port(), _free_port()) for i in (1, 2, 3, 4)]
    control = _free_port()
    sim = Simulator(specs, control, boot_seconds=0.3).start()
    text = (ROOT / "infra" / "test" / "sim-lab.yaml").read_text().replace("127.0.0.1:9100", f"127.0.0.1:{control}")
    for i, spec in enumerate(specs, 1):
        text = text.replace(f"91{i}1", str(spec.http_port)).replace(f"91{i}2", str(spec.bmc_port))
    iac = tmp_path / "sim-lab.yaml"
    iac.write_text(text)
    time.sleep(0.4)
    run = service.execute_run(engine, settings, service.create_run(engine, "test", False), iac, dry_run=False)
    assert run.status == "success", run.log
    assert "PowerState=On" in run.log
    yield sim
    sim.stop()


def test_simulator_faults_and_monitoring(lab, engine, settings):
    assert set(asyncio.run(Monitor(engine, settings).sweep()).values()) == {"up"}
    lab.inject("sim-sensor-02", "overheat")
    lab.inject("sim-sensor-03", "crash")
    statuses = asyncio.run(Monitor(engine, settings).sweep())
    assert statuses["sim-lab.host.sim-sensor-01"] == "up"
    assert statuses["sim-lab.host.sim-sensor-02"] == "degraded"  # health 503 + BMC warning
    assert statuses["sim-lab.host.sim-sensor-03"] == "degraded"  # firmware dead, BMC still fine
    with pytest.raises(ValueError):
        lab.inject("sim-sensor-01", "meteor")


def test_checkout_until_pool_is_busy_then_release_restores(lab, engine):
    leases = [rigs.checkout(engine, "sim-sensor", f"job-{i}") for i in range(4)]
    assert sorted(lease["device"] for lease in leases) == [f"sim-sensor-0{i}" for i in (1, 2, 3, 4)]
    assert leases[0]["endpoints"]["http"].startswith("http://127.0.0.1:")
    with pytest.raises(rigs.RigError) as e:
        rigs.checkout(engine, "sim-sensor", "job-4")
    assert e.value.status == 409 and e.value.detail["busy"] == 4

    device = lab.devices[leases[0]["device"]]
    before = device.booted_at
    released = rigs.release(engine, leases[0]["leaseId"], "passed")
    assert released["status"] == "released" and "power-cycled" in released["note"]
    assert device.booted_at > before  # restored through the BMC
    with pytest.raises(rigs.RigError, match="already released"):
        rigs.release(engine, leases[0]["leaseId"], "passed")


def test_checkout_heals_crashed_device(lab, engine):
    for name in lab.devices:
        lab.inject(name, "crash")
    lease = rigs.checkout(engine, "sim-sensor", "job", heal_wait=5)
    assert "power-cycled via BMC" in lease["note"] and "healthy after recovery" in lease["note"]
    assert lab.devices[lease["device"]].fault is None
    with Session(engine) as s:
        health = s.exec(select(DeviceHealth).where(DeviceHealth.address == lease["address"])).one()
        assert health.consecutive_failures == 0 and not health.quarantined


def test_overheated_device_is_quarantined_not_handed_out(lab, engine):
    for name in lab.devices:
        lab.inject(name, "overheat")
    with pytest.raises(rigs.RigError) as e:
        rigs.checkout(engine, "sim-sensor", "job", heal_wait=1)
    assert e.value.status == 409
    assert all(t["quarantined"] for t in e.value.detail["tried"])  # reboot did not fix hardware
    with pytest.raises(rigs.RigError) as again:
        rigs.checkout(engine, "sim-sensor", "job", heal_wait=1)
    assert again.value.detail["quarantined"] == 4 and again.value.detail["tried"] == []

    lab.inject("sim-sensor-01", "none")
    rigs.unquarantine(engine, "sim-lab.host.sim-sensor-01")
    assert rigs.checkout(engine, "sim-sensor", "job")["device"] == "sim-sensor-01"


def test_infra_failed_result_counts_as_failed_health_check(lab, engine):
    lease = rigs.checkout(engine, "sim-sensor", "job")
    rigs.release(engine, lease["leaseId"], "infra-failed")
    with Session(engine) as s:
        health = s.exec(select(DeviceHealth).where(DeviceHealth.address == lease["address"])).one()
        assert health.consecutive_failures == 1 and not health.quarantined
    # the next checkout re-checks the (restored) device and clears the count
    again = rigs.checkout(engine, "sim-sensor", "job")
    with Session(engine) as s:
        assert s.exec(select(DeviceHealth).where(DeviceHealth.address == again["address"])).one().consecutive_failures == 0


def test_expired_leases_return_devices(lab, engine, settings):
    lease = rigs.checkout(engine, "sim-sensor", "job", ttl_seconds=60)
    with Session(engine) as s:
        row = s.get(Lease, lease["leaseId"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    asyncio.run(Monitor(engine, settings).sweep())
    with Session(engine) as s:
        assert s.get(Lease, lease["leaseId"]).status == "expired"


def test_rig_api_and_page(lab, settings, engine):
    settings.api_token = "t0ken"
    auth = {"X-LabForge-Token": "t0ken"}
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/v1/rigs/checkout", json={"pool": "sim-sensor", "holder": "ci"}).status_code == 401
        r = c.post("/api/v1/rigs/checkout", json={"pool": "sim-sensor", "holder": "ci#1"}, headers=auth)
        assert r.status_code == 201
        lease = r.json()
        assert c.post("/api/v1/rigs/checkout", json={"pool": "nope", "holder": "ci"}, headers=auth).status_code == 404
        pools = c.get("/api/v1/rigs").json()
        assert pools[0]["pool"] == "sim-sensor" and [d["rig"] for d in pools[0]["devices"]].count("leased") == 1
        assert c.get("/api/v1/rigs/leases", params={"active": True}).json()[0]["leaseId"] == lease["leaseId"]

        page = c.get("/rigs").text
        assert "Pool <span class=\"mono\">sim-sensor</span>" in page and "ci#1" in page

        r = c.post(f"/api/v1/rigs/leases/{lease['leaseId']}/release", json={"result": "bogus"}, headers=auth)
        assert r.status_code == 400
        r = c.post(f"/api/v1/rigs/leases/{lease['leaseId']}/release", json={"result": "passed"}, headers=auth)
        assert r.json()["status"] == "released"

        r = c.post("/rigs/checkout", data={"pool": "sim-sensor", "holder": "me", "ttl_minutes": 15},
                   follow_redirects=False)
        assert r.status_code == 303 and "checked+out" in r.headers["location"]


def test_flash_installs_build_and_waits_until_healthy(lab, engine):
    lease = rigs.checkout(engine, "sim-sensor", "job")
    result = rigs.flash(engine, lease["leaseId"], "2.0.0-abc123", sha256="f" * 64, size=123, wait_seconds=10)
    assert result["running"]["firmware"] == "2.0.0-abc123"
    assert result["running"]["artifact_sha256"] == "f" * 64
    status = rigs.device_status(engine, lease["leaseId"])
    assert status["healthy"] and status["version"]["firmware"] == "2.0.0-abc123"
    released = rigs.release(engine, lease["leaseId"], "passed")
    assert "flashed 2.0.0-abc123 (sha256 ffffffffffff)" in released["note"]
    with pytest.raises(rigs.RigError, match="not active"):
        rigs.flash(engine, lease["leaseId"], "2.0.1")


def test_flash_reports_a_device_that_never_comes_back(lab, engine):
    lease = rigs.checkout(engine, "sim-sensor", "job")
    device = lab.devices[lease["device"]]
    device.booted_at, device.boot_seconds = 0, 5  # up now; the flash reboot takes longer than we wait
    with pytest.raises(rigs.RigError) as e:
        rigs.flash(engine, lease["leaseId"], "2.0.0-slow", wait_seconds=1)
    assert e.value.status == 504 and "did not come back healthy" in e.value.detail["error"]


def test_board_without_bmc_heals_through_the_pdu(lab, engine):
    for name in ("sim-sensor-01", "sim-sensor-02", "sim-sensor-03"):
        rigs.checkout(engine, "sim-sensor", f"hold-{name}")  # leave only the budget board free
    lab.inject("sim-sensor-04", "crash")
    lease = rigs.checkout(engine, "sim-sensor", "job", heal_wait=5)
    assert lease["device"] == "sim-sensor-04" and "power-cycled via PDU outlet 4" in lease["note"]
    released = rigs.release(engine, lease["leaseId"], "passed")
    assert "restored: power-cycled via PDU outlet 4" in released["note"]


def test_release_restores_known_good_firmware(lab, engine):
    lease = rigs.checkout(engine, "sim-sensor", "job")
    device = lab.devices[lease["device"]]
    rigs.flash(engine, lease["leaseId"], "9.9.9-test", sha256="a" * 64, wait_seconds=10)
    assert device.firmware == "9.9.9-test"
    with Session(engine) as s:
        assert s.get(Lease, lease["leaseId"]).firmware_version == "9.9.9-test"
    released = rigs.release(engine, lease["leaseId"], "passed")
    assert "restored known-good firmware 1.4.2" in released["note"] and device.firmware == "1.4.2"


def test_trends_and_queue_time(lab, engine):
    a = rigs.checkout(engine, "sim-sensor", "job-a", queued_seconds=42)
    b = rigs.checkout(engine, "sim-sensor", "job-b")
    rigs.release(engine, a["leaseId"], "passed")
    rigs.release(engine, b["leaseId"], "infra-failed")
    with Session(engine) as s:
        [pool] = rigs.trends(s, days=7)
    assert pool["pool"] == "sim-sensor" and pool["leases"] == 2
    assert pool["results"] == {"failed": 0, "infra-failed": 1, "passed": 1} and pool["infraShare"] == 0.5
    assert pool["avgWaitSeconds"] == 42 and set(pool["utilisation"]) == {f"sim-sensor-0{i}" for i in (1, 2, 3, 4)}
    assert pool["heldSeconds"][a["device"]] > 0
