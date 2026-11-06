from __future__ import annotations

import io
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT
from labforge.iac.inspect import describe, json_schema, schema_reference
from labforge.iac.loader import load_path, parse_text
from labforge.iac.workspace import Workspace, WorkspaceError
from labforge.main import create_app

VALID = (ROOT / "examples" / "local" / "local.yaml").read_text()


@pytest.fixture
def ws(tmp_path) -> Workspace:
    root = tmp_path / "iac"
    shutil.copytree(ROOT / "examples" / "local", root)
    return Workspace(root, tmp_path / "history")


@pytest.fixture
def client(settings, tmp_path):
    root = tmp_path / "iac"
    shutil.copytree(ROOT / "examples" / "local", root)
    settings.iac_path = root
    settings.api_token = "t0ken"
    with TestClient(create_app(settings)) as c:
        yield c


# --- workspace --------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape.yaml", "/etc/passwd.yaml", "a/../../x.yaml", "notes.txt", ".hidden.yaml", ""])
def test_rejects_unsafe_paths(ws, bad):
    with pytest.raises(WorkspaceError):
        ws.resolve(bad)


def test_write_conflict_and_history(ws):
    text, base = ws.read("local.yaml")
    new_base = ws.write("local.yaml", text + "\n# edit 1\n", base)
    with pytest.raises(WorkspaceError, match="changed by someone else") as e:
        ws.write("local.yaml", text + "\n# stale edit\n", base)
    assert e.value.status == 409
    ws.write("local.yaml", text + "\n# edit 2\n", new_base)
    revs = ws.history("local.yaml")
    assert len(revs) == 2
    assert ws.revision("local.yaml", revs[-1]["id"]) == text  # oldest snapshot is the original


def test_create_refuses_existing_and_delete_keeps_copy(ws):
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.write("local.yaml", VALID, create=True)
    ws.delete("local.yaml")
    assert ws.files() == []
    assert ws.revision("local.yaml", ws.history("local.yaml")[0]["id"]) == VALID


def test_import_zip_strips_folder_and_rejects_bad_entries(ws):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("infra/a.yaml", VALID.replace("name: demo", "name: a"))
        z.writestr("infra/sub/b.yml", "not: [valid")
        z.writestr("infra/README.md", "ignored")
        z.writestr("infra/__MACOSX/c.yaml", "ignored")
    results = {r.path: r for r in ws.import_upload("lab.zip", buf.getvalue())}
    assert results["a.yaml"].status == "created" and results["a.yaml"].detail == "valid"
    assert results["sub/b.yml"].status == "created" and "problem" in results["sub/b.yml"].detail
    assert set(results) == {"a.yaml", "sub/b.yml"}

    again = ws.import_upload("local.yaml", VALID.encode())
    assert again[0].status == "skipped"
    assert ws.import_upload("local.yaml", VALID.encode(), overwrite=True)[0].status == "updated"
    assert ws.import_upload("x.yaml", b"\xff\xfe")[0].status == "rejected"


def test_export_roundtrip(ws):
    with zipfile.ZipFile(io.BytesIO(ws.export_zip())) as z:
        assert z.namelist() == ["local.yaml"]
        assert z.read("local.yaml").decode() == VALID


# --- parsing views ----------------------------------------------------------


def test_diagnostics_point_at_lines():
    text = VALID.replace("      connection: { type: none }\n      monitor:\n        agent: true",
                         "      connection: { type: none }\n      colour: red\n      monitor:\n        agent: true", 1)
    _, diags = parse_text(text, "x.yaml")
    line = next(i + 1 for i, ln in enumerate(text.splitlines()) if "colour" in ln)
    assert [(d.line, "Extra inputs" in d.message) for d in diags] == [(line, True)]


def test_describe_and_schema():
    envs = describe(load_path(ROOT / "infra"))
    dev = next(e for e in envs if e.name == "dev")
    lb = next(r for r in dev.resources if r.kind == "loadbalancer")
    assert set(lb.depends_on) == {"arm-node-01", "arm-node-02", "lab-vpc", "svc-01"}
    assert "dev_host_svc_01 --> dev_loadbalancer_test_api" in dev.mermaid
    hosts = next(s for s in schema_reference() if s["path"] == "hosts[]")
    platform = next(f for f in hosts["fields"] if f["name"] == "platform")
    assert platform["required"] and platform["type"] == "→ hosts[].platform"
    assert "loadBalancers" in json_schema()["$defs"]["EnvironmentSpec"]["properties"]


# --- web ----------------------------------------------------------------------


def test_workspace_pages(client):
    page = client.get("/iac").text
    assert "IaC workspace" in page and "local.yaml" in page and "Plan for the whole workspace" in page
    editor = client.get("/iac/edit", params={"path": "local.yaml"})
    assert editor.status_code == 200 and "CodeMirror" in editor.text
    assert client.get("/iac/edit", params={"path": "../x.yaml"}).status_code == 400
    assert "hosts[].bmc" in client.get("/iac/schema").text
    assert client.get("/iac/schema.json").json()["title"] == "LabForge environment"


def test_check_reports_problems_structure_and_plan(client):
    ok = client.post("/iac/check", json={"path": "local.yaml", "text": VALID}).json()
    assert ok["diagnostics"] == [] and ok["envs"][0]["name"] == "demo"
    assert ok["plan"]["counts"]["create"] == 4

    bad = client.post("/iac/check", json={"path": "local.yaml", "text": VALID.replace("arm64", "sparc", 1)}).json()
    assert bad["plan"] is None and "arch" in bad["diagnostics"][0]["message"]

    dup = client.post("/iac/check", json={"path": "copy.yaml", "text": VALID}).json()
    assert "already defined in local.yaml" in dup["diagnostics"][0]["message"]


def test_save_new_import_delete(client):
    text = client.get("/api/v1/iac/files/local.yaml").json()["text"]
    first = client.post("/iac/save", json={"path": "local.yaml", "text": text + "\n# a\n"}).json()
    stale = client.post("/iac/save", json={"path": "local.yaml", "text": text, "base": "0" * 16})
    assert stale.status_code == 409
    assert client.post("/iac/save", json={"path": "local.yaml", "text": text, "base": first["digest"]}).status_code == 200
    assert len(client.get("/iac/history", params={"path": "local.yaml"}).json()) == 2

    r = client.post("/iac/new", data={"name": "rigs", "env": "rigs", "starter": "device-pool"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/iac/edit?path=rigs.yaml"
    files = {f["path"]: f for f in client.get("/api/v1/iac/files").json()}
    assert files["rigs.yaml"]["environments"] == ["rigs"] and files["rigs.yaml"]["problems"] == []

    page = client.post("/iac/import", files=[("files", ("extra.yaml", VALID.replace("name: demo", "name: extra")))])
    assert "Import results" in page.text and "created" in page.text
    assert client.post("/iac/delete", data={"path": "extra.yaml"}, follow_redirects=False).status_code == 303
    assert "extra.yaml" not in {f["path"] for f in client.get("/api/v1/iac/files").json()}


def test_apply_single_file_dry_run(client):
    r = client.post("/iac/apply", data={"path": "local.yaml", "dry_run": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/iac/apply", data={"path": "../x.yaml", "dry_run": "true"}).status_code == 400


def test_api_put_requires_token_and_honours_if_match(client):
    assert client.put("/api/v1/iac/files/new.yaml", content=VALID).status_code == 401
    headers = {"X-LabForge-Token": "t0ken", "Content-Type": "text/plain"}
    created = client.put("/api/v1/iac/files/new.yaml", content=VALID.replace("name: demo", "name: new"),
                         headers=headers).json()
    assert created["problems"] == []
    stale = client.put("/api/v1/iac/files/new.yaml", content=VALID, headers={**headers, "If-Match": "0" * 16})
    assert stale.status_code == 409
    v = client.post("/api/v1/iac/validate", json={"text": "apiVersion: labforge/v1\nkind: Environment\n"}).json()
    assert not v["valid"] and v["problems"][0]["message"].endswith("Field required")


def test_editing_can_be_disabled(settings, tmp_path):
    root = tmp_path / "ro"
    shutil.copytree(ROOT / "examples" / "local", root)
    settings.iac_path, settings.ui_edit = root, False
    with TestClient(create_app(settings)) as c:
        assert c.post("/iac/save", json={"path": "local.yaml", "text": VALID}).status_code == 403
        assert c.post("/iac/new", data={"name": "x"}).status_code == 403
        assert "Read-only" in c.get("/iac").text
