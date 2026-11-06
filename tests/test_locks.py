"""Several LabForge instances on one database: locks, monitor leadership, serialized applies."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from sqlmodel import Session

from conftest import ROOT
from labforge import locks, service
from labforge.db import make_engine
from labforge.models import DbLock, utcnow
from labforge.monitor.sweep import Monitor


def test_lease_locks_between_instances(engine):
    assert locks.acquire(engine, "x", 60, owner="a")
    assert not locks.acquire(engine, "x", 60, owner="b")
    assert locks.acquire(engine, "x", 60, owner="a")  # renewal by the holder
    assert locks.holder(engine, "x") == "a"
    with Session(engine) as s:  # the holder dies: its lease runs out
        row = s.get(DbLock, "x")
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    assert locks.acquire(engine, "x", 60, owner="b") and locks.holder(engine, "x") == "b"
    locks.release(engine, "x", owner="a")  # not the holder: no effect
    assert locks.holder(engine, "x") == "b"
    with pytest.raises(locks.LockBusy, match="held by b"):
        with locks.held(engine, "x", 60, wait_seconds=0.3, owner="c"):
            pass


def test_only_the_monitor_leader_sweeps(engine, settings):
    a, b = Monitor(engine, settings), Monitor(engine, settings)
    assert asyncio.run(a.sweep_if_leader(owner="instance-a")) == {}
    assert asyncio.run(b.sweep_if_leader(owner="instance-b")) is None


def test_apply_waits_for_another_instance(engine, settings):
    locks.acquire(engine, "apply", 600, owner="other-host:123")
    run_id = service.create_run(engine, "test", dry_run=True)
    with pytest.raises(service.BusyError, match="other-host:123"):
        service.execute_run(engine, settings, run_id, ROOT / "examples" / "local", dry_run=True)
    locks.release(engine, "apply", owner="other-host:123")
    run = service.execute_run(engine, settings, service.create_run(engine, "test", True), ROOT / "examples" / "local",
                              dry_run=True)
    assert run.status == "success" and locks.holder(engine, "apply") is None


@pytest.mark.skipif(not os.environ.get("LABFORGE_TEST_POSTGRES_URL"), reason="set LABFORGE_TEST_POSTGRES_URL")
def test_postgres_end_to_end(settings):
    """Schema, migrations, locks, apply and a monitor sweep on PostgreSQL."""
    url = os.environ["LABFORGE_TEST_POSTGRES_URL"]
    engine = make_engine(url)
    make_engine(url)  # second start: create_all + additive migration are idempotent
    settings.db_url = url
    with Session(engine) as s:
        s.query(DbLock).delete()
        s.commit()
    assert locks.acquire(engine, "pg", 60, owner="a") and not locks.acquire(engine, "pg", 60, owner="b")
    locks.release(engine, "pg", owner="a")
    run = service.execute_run(engine, settings, service.create_run(engine, "pg", False), ROOT / "examples" / "local",
                              dry_run=False)
    assert run.status == "success", run.log
    statuses = asyncio.run(Monitor(engine, settings).sweep())
    assert statuses["local.host.offline-board"] == "down"


def test_failing_lock_release_does_not_leak_the_process_lock(engine, settings, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(locks, "release", broken)
    run = service.execute_run(engine, settings, service.create_run(engine, "t", True), ROOT / "examples" / "local",
                              dry_run=True)
    assert run.status == "success"
    assert service._apply_lock.acquire(blocking=False)  # still free for the next apply
    service._apply_lock.release()
