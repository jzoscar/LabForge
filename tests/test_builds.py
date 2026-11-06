from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import select

from conftest import RecordingRunner, write_env
from labforge import builds
from labforge.iac.applier import ApplyContext, apply_plan
from labforge.iac.loader import load_path
from labforge.iac.planner import build_plan
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Container
from labforge.main import create_app
from labforge.models import ResourceState


def report(run_id: str, *, conan: str = "warm", deps: float = 2.0, build: float = 4.0, test: float = 1.0,
           hits: int = 3, calls: int = 3, ok: bool = True, preset: str = "linux-arm64") -> dict:
    stages = [{"name": "deps", "seconds": deps, "returncode": 0},
              {"name": "configure", "seconds": 1.0, "returncode": 0},
              {"name": "cache-zero", "seconds": 0.5, "returncode": 0},
              {"name": "build", "seconds": build, "returncode": 0 if ok else 1},
              {"name": "cache-stats", "seconds": 0.5, "returncode": 0},
              {"name": "test", "seconds": test, "returncode": 0}]
    return {"run_id": run_id, "kind": "build", "project_dir": "/src/native", "ok": ok, "exit_code": 0 if ok else 1,
            "started_at": f"2026-11-05T18:{len(run_id):02d}:00-07:00", "stages": stages,
            "meta": {"preset": preset, "cache": {"conan": conan, "compiler_cache": "ccache"},
                     "compiler_cache_stats": {"hits": hits, "misses": calls - hits, "cacheable_calls": calls}}}


def test_ingest_stats_and_suggestions(session):
    builds.ingest(session, report("cold-1", conan="cold", deps=24, hits=0), runner="r1")
    for i in range(4):
        builds.ingest(session, report(f"warm-{i}", hits=1), runner="r1", queued_seconds=200)
    _, created = builds.ingest(session, report("warm-0", hits=1), runner="r1")
    assert not created  # same run from the same runner updates instead of duplicating

    [p] = builds.stats(session)
    assert p["pipeline"] == "linux-arm64" and p["runs"] == 5 and p["successRate"] == 1.0
    assert p["depsColdSeconds"] == 24 and p["depsWarmSeconds"] == 2
    assert p["compilerCacheHitRate"] == round(4 / 15, 3)
    assert p["stages"][0]["name"] == "deps" and p["medianQueueSeconds"] == 200
    texts = " | ".join(s["text"] for s in builds.suggestions([p]))
    assert "24s cold vs 2.0s warm (12x)" in texts and "Conan remote" in texts
    assert "Compiler cache hit rate is 27%" in texts
    assert "wait 200s (median)" in texts and "replicas" in texts


def test_failures_and_test_heavy_pipelines(session):
    for i in range(5):
        builds.ingest(session, report(f"t{i}", preset="host-debug", deps=0.5, build=1, test=8, ok=i > 1))
    [p] = builds.stats(session)
    texts = " | ".join(s["text"] for s in builds.suggestions([p]))
    assert "2 of 5 runs failed" in texts and "ctest -j" in texts


def test_report_files_finds_runs(tmp_path):
    for name in ("20260101-a", "20260101-b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(json.dumps(report(name)))
    (tmp_path / "latest").symlink_to(tmp_path / "20260101-b")
    assert [p.parent.name for p in builds.report_files(tmp_path)] == ["20260101-a", "20260101-b"]
    assert builds.report_files(tmp_path / "20260101-a") == [tmp_path / "20260101-a" / "report.json"]


def test_builds_api_and_page(settings):
    settings.api_token = "t"
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/v1/builds", json=report("x")).status_code == 401
        assert c.post("/api/v1/builds", json={"nope": 1}, headers={"X-LabForge-Token": "t"}).status_code == 422
        r = c.post("/api/v1/builds", json=report("x"), params={"runner": "ci-1"}, headers={"X-LabForge-Token": "t"})
        assert r.json()["created"] and r.json()["pipeline"] == "linux-arm64"
        assert c.get("/api/v1/builds").json()[0]["runner"] == "ci-1"
        assert c.get("/api/v1/builds/stats").json()["pipelines"][0]["runs"] == 1
        page = c.get("/builds").text
        assert "<h2>Pipelines</h2>" in page and "linux-arm64" in page and "ci-1" in page


FARM = """apiVersion: labforge/v1
kind: Environment
metadata: {name: farm}
spec:
  hosts:
    - {name: b1, platform: {os: linux}, address: 10.0.0.5, connection: {type: ssh, user: ci}}
  containers:
    - {name: agent, host: b1, image: runner:1, replicas: REPLICAS}
"""


def test_replicas_scale_up_and_down(tmp_path, session):
    def apply(n: int) -> list[list[str]]:
        docs = load_path(write_env(tmp_path / "farm.yaml", FARM.replace("REPLICAS", str(n))))
        plan = build_plan(docs, list(session.exec(select(ResourceState))))
        runner = RecordingRunner(False, lambda _l: None)
        ctx = ApplyContext(runner=runner, secrets=SecretResolver(environ={}), log=lambda _l: None,
                           desired=plan.desired)
        apply_plan(plan, session, ctx)
        return [c["argv"][3:] for c in runner.calls]

    up = apply(3)
    assert [a[a.index("--name") + 1] for a in up if a[0] == "run"] == ["agent-1", "agent-2", "agent-3"]
    down = apply(2)
    assert ["rm", "-f", "agent-3"] in down and not any(a[0] == "run" and "agent-3" in a for a in down)


def test_replicas_cannot_publish_fixed_ports():
    with pytest.raises(ValidationError, match="fixed host ports"):
        Container(name="a", host="h", image="i", ports=["8080:8080"], replicas=2)
    assert Container(name="a", host="h", image="i", ports=["8080"], replicas=2).instance_names == ["a-1", "a-2"]
