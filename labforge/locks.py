"""Locks shared by every LabForge instance on one database (SQLite or PostgreSQL).

Each lock is a row with an owner and an expiry. Taking it succeeds when nobody holds it, when the
holder's lease ran out (that instance died), or when we already hold it (renewal). Used to elect one
monitor leader and to serialize applies and rig checkouts across instances.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from labforge.models import DbLock, utcnow

INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class LockBusy(Exception):
    pass


def acquire(engine: Engine, name: str, ttl_seconds: float, owner: str = INSTANCE) -> bool:
    now = utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    with Session(engine) as s:
        if s.get(DbLock, name) is None:
            s.add(DbLock(name=name, owner=owner, expires_at=expires))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()  # another instance inserted it first; fall through and compete
        result = s.execute(update(DbLock).where(DbLock.name == name,
                                                or_(DbLock.owner == owner, DbLock.expires_at < now))
                           .values(owner=owner, expires_at=expires))
        s.commit()
        return result.rowcount == 1


def release(engine: Engine, name: str, owner: str = INSTANCE) -> None:
    with Session(engine) as s:
        s.execute(delete(DbLock).where(DbLock.name == name, DbLock.owner == owner))
        s.commit()


def holder(engine: Engine, name: str) -> str | None:
    with Session(engine) as s:
        row = s.get(DbLock, name)
        return row.owner if row and row.expires_at > utcnow() else None


@contextmanager
def held(engine: Engine, name: str, ttl_seconds: float, wait_seconds: float = 0.0, owner: str = INSTANCE):
    """Hold a lock for a block of work, waiting up to `wait_seconds` for it."""
    deadline = time.monotonic() + wait_seconds
    while not acquire(engine, name, ttl_seconds, owner):
        if time.monotonic() >= deadline:
            raise LockBusy(f"{name} is held by {holder(engine, name) or 'another instance'}")
        time.sleep(0.2)
    try:
        yield
    finally:
        release(engine, name, owner)
