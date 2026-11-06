from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT
from labforge import service
from labforge.main import create_app


@pytest.fixture
def client(settings, engine):
    settings.api_token = "t0ken"
    app = create_app(settings)
    run_id = service.create_run(app.state.engine, "test", dry_run=False)
    service.execute_run(app.state.engine, settings, run_id, ROOT / "examples" / "local", dry_run=False)
    with TestClient(app) as c:
        yield c


AUTH = {"X-LabForge-Token": "t0ken"}
REPORT = {"env": "demo", "name": "this-mac", "os": "darwin", "arch": "arm64",
          "cpu_percent": 12.5, "mem_percent": 40, "disk_percent": 55, "temp_c": 48}


def test_agent_report_requires_token(client):
    assert client.post("/api/v1/agent/report", json=REPORT).status_code == 401
    r = client.post("/api/v1/agent/report", json=REPORT, headers=AUTH)
    assert r.json() == {"address": "demo.host.this-mac", "managed": True}
    r = client.post("/api/v1/agent/report", json={**REPORT, "name": "stranger"}, headers=AUTH)
    assert r.json()["managed"] is False
    metrics = client.get("/api/v1/resources/demo.host.this-mac/metrics").json()
    assert metrics[0]["cpu_percent"] == 12.5


def test_resources_and_plan(client):
    rows = client.get("/api/v1/resources", params={"kind": "host"}).json()
    assert {r["address"] for r in rows} == {"demo.host.this-mac", "demo.host.offline-board"}
    assert client.get("/api/v1/resources/nope").status_code == 404
    assert client.post("/api/v1/iac/plan", json={}).status_code == 401
    plan = client.post("/api/v1/iac/plan", json={}, headers=AUTH).json()
    assert plan["counts"]["noop"] == 4 and plan["counts"]["create"] == 0


def test_api_apply_dry_run(client):
    run_id = client.post("/api/v1/iac/apply", json={"path": str(ROOT / "infra")}, headers=AUTH).json()["runId"]
    for _ in range(100):
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "success" and run["dry_run"] is True


def test_pages_render(client):
    client.post("/api/v1/agent/report", json=REPORT, headers=AUTH)
    for url in ["/", "/partials/dashboard", "/resources", "/resources?kind=host&q=mac",
                "/resources/demo.host.this-mac", "/resources/demo.loadbalancer.ui", "/iac", "/runs", "/runs/1"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Physical test hardware" in client.get("/").text
    for url in ["/iac", "/runs"]:
        assert "built-in method" not in client.get(url).text, url


def test_ui_real_apply_disabled(client):
    r = client.post("/iac/apply", data={}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/iac/apply", data={"dry_run": "true"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
