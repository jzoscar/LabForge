"""GitOps end to end: a real LabForge server on a free port, driven by the real CLI."""

from __future__ import annotations

import shutil
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from conftest import ROOT
from labforge.cli import app as cli
from labforge.main import create_app


@pytest.fixture
def server(settings, tmp_path):
    workspace = tmp_path / "server-iac"
    workspace.mkdir()
    (workspace / "stale.yaml").write_text((ROOT / "examples" / "local" / "local.yaml").read_text()
                                          .replace("name: local", "name: stale"))
    settings.iac_path, settings.api_token = workspace, "admin-token"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/api/v1/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url, workspace
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "examples" / "local", repo / "envs")
    return repo


def run(*args: str):
    return CliRunner().invoke(cli, list(args), env={"LABFORGE_API_TOKEN": "admin-token"})


def test_remote_plan_is_markdown_against_server_state(server, checkout):
    url, _ = server
    r = run("plan", str(checkout), "--remote", url, "--format", "markdown")
    assert r.exit_code == 0, r.output
    assert "### LabForge plan" in r.output and "| `+` | `local.host.this-mac` | dev |" in r.output


def test_sync_prune_and_apply(server, checkout):
    url, workspace = server
    r = run("sync", str(checkout), "--remote", url)
    assert r.exit_code == 0, r.output
    assert "~ envs/local.yaml" in r.output and "! stale.yaml exists only on the server" in r.output
    assert (workspace / "envs" / "local.yaml").is_file() and (workspace / "stale.yaml").is_file()

    r = run("sync", str(checkout), "--remote", url, "--prune", "--apply")
    assert r.exit_code == 0, r.output
    assert "= envs/local.yaml" in r.output and "- stale.yaml" in r.output
    assert "==> create local.host.this-mac" in r.output and "apply complete" in r.output
    assert not (workspace / "stale.yaml").exists()

    again = run("plan", str(checkout), "--remote", url, "--format", "markdown")
    assert "No changes." in again.output  # merged = applied: the server state matches git


def test_remote_plan_rejects_unsafe_paths(server):
    url, _ = server
    r = httpx.post(f"{url}/api/v1/iac/plan", json={"files": {"../escape.yaml": "x"}},
                   headers={"X-LabForge-Token": "admin-token"})
    assert r.status_code == 400
