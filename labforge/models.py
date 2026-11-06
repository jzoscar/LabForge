"""Persistent state: IaC resource state, apply runs, probe results, events, agent metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(UTC)


class ResourceState(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    address: str = Field(index=True, unique=True)  # "<env>.<kind>.<name>"
    env: str = Field(index=True)
    tier: str
    kind: str = Field(index=True)
    name: str
    spec_json: str
    spec_hash: str
    outputs_json: str = "{}"
    status: str = "unknown"  # up | degraded | down | unknown
    status_detail: str = ""
    checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def spec(self) -> dict:
        return json.loads(self.spec_json)

    @property
    def outputs(self) -> dict:
        return json.loads(self.outputs_json or "{}")


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    action: str = "apply"  # apply | destroy
    source: str = ""
    dry_run: bool = True
    status: str = "running"  # running | success | failed
    summary_json: str = "{}"
    log: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def summary(self) -> dict:
        return json.loads(self.summary_json or "{}")


class CheckResult(SQLModel, table=True):
    """Latest result per (resource, probe)."""

    __table_args__ = (UniqueConstraint("address", "probe"),)

    id: int | None = Field(default=None, primary_key=True)
    address: str = Field(index=True)
    probe: str
    ok: bool | None = None  # None = could not determine
    latency_ms: float | None = None
    detail: str = ""
    ts: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """Resource status transitions."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    address: str = Field(index=True)
    old_status: str
    new_status: str
    detail: str = ""


class AgentReport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    address: str = Field(index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    os: str | None = None
    arch: str | None = None
    cpu_percent: float
    mem_percent: float
    disk_percent: float | None = None
    temp_c: float | None = None
    payload_json: str = "{}"

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")


class Lease(SQLModel, table=True):
    """A device checked out of a rig pool, usually by a CI job."""

    id: int | None = Field(default=None, primary_key=True)
    address: str = Field(index=True)  # resource address of the device
    pool: str = Field(index=True)
    holder: str  # who has it, e.g. "github:org/repo#1234"
    purpose: str = ""
    status: str = Field(default="active", index=True)  # active | released | expired
    result: str | None = None  # passed | failed | infra-failed
    acquired_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    released_at: datetime | None = None
    note: str = ""
    # Added after the first release: nullable so existing databases migrate in place.
    queued_seconds: float | None = None  # how long the job waited for a free device
    firmware_version: str | None = None  # last build flashed during this lease
    firmware_sha256: str | None = None


class DeviceHealth(SQLModel, table=True):
    """Rig bookkeeping per device: consecutive failed health checks and quarantine."""

    id: int | None = Field(default=None, primary_key=True)
    address: str = Field(index=True, unique=True)
    consecutive_failures: int = 0
    quarantined: bool = False
    reason: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
