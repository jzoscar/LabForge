"""Secrets are referenced by name in IaC and resolved from LABFORGE_SECRET_<NAME> env vars."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


class SecretError(Exception):
    pass


def env_key(ref: str) -> str:
    return "LABFORGE_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()


class SecretResolver:
    def __init__(self, dry_run: bool = False, environ: Mapping[str, str] | None = None):
        self.dry_run = dry_run
        self.environ = os.environ if environ is None else environ

    def get_optional(self, ref: str | None) -> str | None:
        return self.environ.get(env_key(ref)) if ref else None

    def get(self, ref: str) -> str:
        value = self.get_optional(ref)
        if value is not None:
            return value
        if self.dry_run:
            return f"<secret:{ref}>"
        raise SecretError(f"missing secret {ref!r}: set {env_key(ref)}")

    def credentials(self, ref: str) -> tuple[str, str]:
        if self.dry_run and self.get_optional(ref) is None:
            return f"<user:{ref}>", f"<password:{ref}>"
        user, sep, password = self.get(ref).partition(":")
        if not sep and not self.dry_run:
            raise SecretError(f"secret {ref!r} must be in 'user:password' form")
        return user, password
