from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import ROOT
from labforge.auth import create_token, revoke_token
from labforge.main import create_app
from labforge.models import ApiToken, AuditEvent

REPORT = {"env": "local", "name": "this-mac", "cpu_percent": 1, "mem_percent": 2}
LOCAL = (ROOT / "examples" / "local" / "local.yaml").read_text()


@pytest.fixture
def app(settings, tmp_path):
    root = tmp_path / "iac"
    shutil.copytree(ROOT / "examples" / "local", root)
    settings.iac_path = root
    return create_app(settings)


def tok(app, role, **kw) -> dict:
    return {"X-LabForge-Token": create_token(app.state.engine, f"{role}-{len(kw)}-{id(kw)}", role, **kw)}


def test_open_until_a_token_exists(app):
    with TestClient(app) as c:
        assert c.post("/api/v1/agent/report", json=REPORT).status_code == 200  # development: open
        create_token(app.state.engine, "ci", "operator")
        assert c.post("/api/v1/agent/report", json=REPORT).status_code == 401  # now tokens are required


def test_roles_and_environment_scope(app):
    with TestClient(app) as c:
        viewer, operator = tok(app, "viewer"), tok(app, "operator")
        editor, admin = tok(app, "editor"), tok(app, "admin")
        scoped = tok(app, "operator", envs=["sim-*"])

        assert c.post("/api/v1/iac/plan", json={}, headers=viewer).status_code == 200
        assert c.post("/api/v1/builds", json={"run_id": "x", "stages": []}, headers=viewer).status_code == 403
        assert c.post("/api/v1/builds", json={"run_id": "x", "stages": []}, headers=operator).status_code == 201

        other, text = LOCAL.replace("name: local", "name: other"), {"Content-Type": "text/plain"}
        assert c.put("/api/v1/iac/files/o.yaml", content=other, headers={**text, **operator}).status_code == 403
        assert c.put("/api/v1/iac/files/o.yaml", content=other, headers={**text, **editor}).status_code == 200

        dry = {"dry_run": True}
        assert c.post("/api/v1/iac/apply", json=dry, headers=operator).status_code == 202
        r = c.post("/api/v1/iac/apply", json={"dry_run": False}, headers=operator)
        assert r.status_code == 403 and "needs admin" in r.json()["detail"]
        r = c.post("/api/v1/iac/apply", json=dry, headers=scoped)
        assert r.status_code == 403 and "may not change environment(s) local, other" in r.json()["detail"]
        assert c.post("/api/v1/iac/apply", json={"dry_run": False}, headers=admin).status_code == 202

        assert c.get("/api/v1/tokens", headers=operator).status_code == 403
        listed = c.get("/api/v1/tokens", headers=admin).json()
        assert listed and "token_hash" not in listed[0]


def test_agent_tokens_report_only_as_their_host(app):
    with TestClient(app) as c:
        agent = tok(app, "agent", subject="local.host.this-mac")
        assert c.post("/api/v1/agent/report", json=REPORT, headers=agent).status_code == 200
        other = {**REPORT, "name": "offline-board"}
        assert c.post("/api/v1/agent/report", json=other, headers=agent).status_code == 403
        assert c.post("/api/v1/rigs/checkout", json={"pool": "p", "holder": "x"}, headers=agent).status_code == 403
        with pytest.raises(ValueError, match="need a subject"):
            create_token(app.state.engine, "bad", "agent")


def test_revoked_tokens_stop_working(app):
    with TestClient(app) as c:
        headers = {"X-LabForge-Token": create_token(app.state.engine, "temp", "operator")}
        assert c.post("/api/v1/agent/report", json=REPORT, headers=headers).status_code == 200
        revoke_token(app.state.engine, "temp")
        assert c.post("/api/v1/agent/report", json=REPORT, headers=headers).status_code == 401
        with Session(app.state.engine) as s:
            row = s.exec(select(ApiToken).where(ApiToken.name == "temp")).one()
            assert row.last_used_at is not None and row.revoked_at is not None


ACCESS = """apiVersion: labforge/v1
kind: AccessPolicy
metadata: {name: access}
spec:
  bindings:
    - {subjects: ["group:lab-admins"], role: admin}
    - {subjects: ["ops@example.com"], role: operator, envs: ["sim-*"]}
"""


def test_sso_users_get_roles_from_the_access_policy(settings, tmp_path):
    root = tmp_path / "iac"
    shutil.copytree(ROOT / "examples" / "local", root)
    (root / "access.yaml").write_text(ACCESS)
    settings.iac_path, settings.auth_header, settings.groups_header = root, "X-Forwarded-Email", "X-Forwarded-Groups"
    app = create_app(settings)
    viewer = {"X-Forwarded-Email": "dev@example.com"}
    admin = {"X-Forwarded-Email": "lead@example.com", "X-Forwarded-Groups": "staff, lab-admins"}
    ops = {"X-Forwarded-Email": "ops@example.com"}
    body = {"path": "local.yaml", "text": LOCAL + "\n# edited\n"}
    with TestClient(app) as c:
        assert c.get("/").status_code == 401  # pages need SSO
        assert c.get("/metrics").status_code == 200  # scraped without SSO
        assert "dev@example.com · viewer" in c.get("/", headers=viewer).text
        assert c.post("/iac/save", json=body, headers=viewer).status_code == 403
        assert c.post("/iac/save", json=body, headers=admin).status_code == 200
        r = c.post("/iac/apply", data={"path": "local.yaml", "dry_run": "true"}, headers=ops, follow_redirects=False)
        assert r.status_code == 403  # operator only for sim-* environments
        assert c.post("/iac/apply", data={"path": "local.yaml", "dry_run": "true"}, headers=admin,
                      follow_redirects=False).status_code == 303

        with Session(app.state.engine) as s:
            events = [(e.actor, e.action) for e in s.exec(select(AuditEvent).order_by(AuditEvent.id))]
        assert ("lead@example.com", "iac.save") in events and ("lead@example.com", "apply.start") in events
        page = c.get("/audit", headers=viewer).text
        assert "iac.save" in page and "lead@example.com" in page
