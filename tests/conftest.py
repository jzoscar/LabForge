from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlmodel import Session

from labforge.config import Settings
from labforge.db import make_engine
from labforge.iac.runner import CmdResult, Runner

ROOT = Path(__file__).resolve().parent.parent


class RecordingRunner(Runner):
    """Executes nothing; records argv/env/input of every command, even when not in dry-run."""

    def __init__(self, dry_run: bool, log):
        super().__init__(dry_run, log)
        self.calls: list[dict] = []

    def run(self, argv, *, input=None, env=None, timeout=900, check=True):
        self.log("$ " + " ".join(argv))
        self.calls.append({"argv": argv, "input": input, "env": env or {}})
        return CmdResult(0, "Chassis Power is on\n" if "ipmitool" in argv[0] else "{}")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # LABFORGE_TEST_DB_URL runs the suite on another database (e.g. PostgreSQL), schema rebuilt per test.
    url = os.environ.get("LABFORGE_TEST_DB_URL")
    if url:
        from sqlmodel import SQLModel, create_engine

        from sqlalchemy import text

        fresh = create_engine(url)
        with fresh.begin() as conn:  # sessions left by earlier tests would block DROP TABLE
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                              "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        SQLModel.metadata.drop_all(fresh)
        fresh.dispose()
    return Settings(db_url=url or f"sqlite:///{tmp_path / 'test.db'}", iac_path=ROOT / "examples" / "local",
                    data_dir=tmp_path / "data", monitor_enabled=False)


@pytest.fixture
def engine(settings: Settings):
    return make_engine(settings.db_url)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def write_env(path: Path, body: str) -> Path:
    path.write_text(body)
    return path
