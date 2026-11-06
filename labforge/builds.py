"""Build-farm speed: BuildPulse run reports in, trends and concrete suggestions out."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from labforge.models import BuildRun, utcnow

# Stages that only exist to measure the compiler cache; their cost is overhead, not building.
BOOKKEEPING = {"ccache-zero", "ccache-stats", "cache-zero", "cache-stats"}


def report_files(path: Path) -> list[Path]:
    """report.json files under a run directory, a log directory, or a single file."""
    if path.is_file():
        return [path]
    direct = path / "report.json"
    if direct.is_file():
        return [direct]
    return sorted(p for p in path.glob("*/report.json") if not p.parent.is_symlink())


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _cache(meta: dict) -> dict:
    cache = meta.get("cache") or {}
    stats = meta.get("compiler_cache_stats") or meta.get("ccache_stats") or {}
    tool = cache.get("compiler_cache") or ("ccache" if cache.get("ccache") else None)
    return {"conan": cache.get("conan"), "compilerCache": tool,
            "hits": stats.get("hits"), "calls": stats.get("cacheable_calls")}


def ingest(session: Session, report: dict, *, runner: str = "", pipeline: str | None = None,
           queued_seconds: float | None = None) -> tuple[BuildRun, bool]:
    """Store one report.json; re-ingesting the same run from the same runner updates it."""
    meta = report.get("meta") or {}
    run_id = report["run_id"]
    row = session.exec(select(BuildRun).where(BuildRun.run_id == run_id, BuildRun.runner == runner)).first()
    created = row is None
    row = row or BuildRun(run_id=run_id, runner=runner, pipeline="")
    stages = [{"name": s["name"], "seconds": float(s.get("seconds") or 0), "returncode": s.get("returncode", 0)}
              for s in report.get("stages", [])]
    row.pipeline = pipeline or meta.get("preset") or (f"device-test:{meta['pool']}" if meta.get("pool") else None) \
        or report.get("kind") or "run"
    row.kind, row.project = report.get("kind", ""), report.get("project_dir", "")
    row.ok = bool(report.get("ok", all(s["returncode"] == 0 for s in stages)))
    row.exit_code = int(report.get("exit_code", 0))
    row.total_seconds = round(sum(s["seconds"] for s in stages), 3)
    row.queued_seconds = queued_seconds if queued_seconds is not None else row.queued_seconds
    row.stages_json, row.cache_json = json.dumps(stages), json.dumps(_cache(meta))
    row.meta_json = json.dumps({k: meta.get(k) for k in ("preset", "build_type", "docker_image", "result", "pool")
                                if meta.get(k) is not None})
    row.started_at = _parse_time(report.get("started_at"))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, created


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def stats(session: Session, days: int = 30) -> list[dict]:
    """Per pipeline: runs, success rate, duration percentiles, stage shares, cache behaviour, queueing."""
    since = utcnow() - timedelta(days=days)
    runs = [r for r in session.exec(select(BuildRun).order_by(BuildRun.id)) if r.ingested_at >= since]
    out = []
    for name in sorted({r.pipeline for r in runs}):
        mine = sorted((r for r in runs if r.pipeline == name),
                      key=lambda r: (r.started_at or r.ingested_at).timestamp())
        ok = [r for r in mine if r.ok]
        totals = [r.total_seconds for r in ok] or [r.total_seconds for r in mine]
        stage_time: dict[str, list[float]] = {}
        for r in ok or mine:
            for s in r.stages:
                stage_time.setdefault(s["name"], []).append(s["seconds"])
        avg_total = statistics.fmean(totals) if totals else 0.0
        stages = sorted(({"name": k, "avgSeconds": round(statistics.fmean(v), 2),
                          "share": round(statistics.fmean(v) / avg_total, 3) if avg_total else 0.0}
                         for k, v in stage_time.items()), key=lambda s: -s["avgSeconds"])
        hits = sum(r.cache.get("hits") or 0 for r in mine)
        calls = sum(r.cache.get("calls") or 0 for r in mine)
        deps = {state: [next((s["seconds"] for s in r.stages if s["name"] == "deps"), None) for r in mine
                        if r.cache.get("conan") == state] for state in ("cold", "warm")}
        deps = {k: [x for x in v if x is not None] for k, v in deps.items()}
        waits = [r.queued_seconds for r in mine if r.queued_seconds is not None]
        out.append({
            "pipeline": name, "runs": len(mine), "failed": len(mine) - len(ok),
            "successRate": round(len(ok) / len(mine), 3) if mine else None,
            "medianSeconds": round(statistics.median(totals), 2) if totals else 0.0,
            "p90Seconds": round(_pct(totals, 0.9), 2),
            "trend": [round(r.total_seconds, 2) for r in mine][-30:],
            "stages": stages,
            "bookkeepingShare": round(sum(s["share"] for s in stages if s["name"] in BOOKKEEPING), 3),
            "compilerCacheHitRate": round(hits / calls, 3) if calls else None,
            "depsColdSeconds": round(statistics.median(deps["cold"]), 2) if deps["cold"] else None,
            "depsWarmSeconds": round(statistics.median(deps["warm"]), 2) if deps["warm"] else None,
            "medianQueueSeconds": round(statistics.median(waits), 1) if waits else None,
            "lastRun": max((r.started_at or r.ingested_at) for r in mine) if mine else None,
        })
    return out


def secs(value: float) -> str:
    return f"{value:.1f}s" if value < 10 else f"{value:.0f}s"


def suggestions(pipelines: list[dict]) -> list[dict]:
    """Concrete, data-backed next steps; each names the pipeline and the numbers behind it."""
    out = []

    def add(p: dict, severity: str, text: str) -> None:
        out.append({"pipeline": p["pipeline"], "severity": severity, "text": text})

    for p in pipelines:
        share = {s["name"]: s["share"] for s in p["stages"]}
        cold, warm = p["depsColdSeconds"], p["depsWarmSeconds"]
        if cold and warm and cold > 3 * warm:
            add(p, "high", f"Dependencies take {secs(cold)} cold vs {secs(warm)} warm ({cold / warm:.0f}x). "
                           "Share a Conan remote (e.g. Artifactory) so every runner starts warm.")
        elif share.get("deps", 0) > 0.3:
            add(p, "high", f"The deps stage is {share['deps']:.0%} of the build. Cache Conan packages "
                           "across runners (remote or persistent cache volume).")
        rate = p["compilerCacheHitRate"]
        if rate is not None and rate < 0.5 and p["runs"] >= 3:
            add(p, "medium", f"Compiler cache hit rate is {rate:.0%}. Check the cache key is stable (paths, "
                             "flags, compiler version) and use shared sccache/ccache storage for CI.")
        if p["bookkeepingShare"] > 0.15:
            add(p, "medium", f"Cache bookkeeping stages take {p['bookkeepingShare']:.0%} of the time. Run them in "
                             "the build container already started for the build instead of a separate start-up.")
        if share.get("test", 0) > 0.4:
            add(p, "medium", f"Tests are {share['test']:.0%} of the time. Run them in parallel (ctest -j) or "
                             "split slow suites into their own job.")
        if share.get("configure", 0) > 0.25:
            add(p, "low", f"Configure is {share['configure']:.0%} of the time. Keep build trees between runs "
                          "where it is safe instead of reconfiguring from scratch.")
        if p["runs"] >= 5 and p["successRate"] is not None and p["successRate"] < 0.8:
            add(p, "high", f"{p['failed']} of {p['runs']} runs failed. Run `buildpulse triage` on them; if failures "
                           "are infrastructure, look at the runners and rigs, not the code.")
        if p["medianQueueSeconds"] and p["medianQueueSeconds"] > 120:
            add(p, "high", f"Jobs wait {secs(p['medianQueueSeconds'])} (median) for a runner. Add build agents "
                           "(raise `replicas` on the runner container and apply).")
        if p["runs"] >= 5 and p["medianSeconds"] and p["p90Seconds"] > 2 * p["medianSeconds"]:
            add(p, "low", f"Duration is unstable: p90 {secs(p['p90Seconds'])} vs median {secs(p['medianSeconds'])}. "
                          "Usually cold caches on some runners, or noisy neighbours.")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda s: (order[s["severity"]], s["pipeline"]))
