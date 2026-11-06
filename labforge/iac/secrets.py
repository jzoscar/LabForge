"""Secrets are referenced by name in IaC and resolved at run time, never stored.

Lookup order for a reference `bmc-default`:
  1. the environment variable LABFORGE_SECRET_BMC_DEFAULT
  2. HashiCorp Vault, when VAULT_ADDR is set: KV v2 at <mount>/data/<prefix>/bmc-default, field "value"
     (mount and prefix default to "secret" and "labforge"; token from VAULT_TOKEN)
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Mapping

import httpx

log = logging.getLogger("labforge.secrets")


class SecretError(Exception):
    pass


def env_key(ref: str) -> str:
    return "LABFORGE_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()


class VaultClient:
    """Minimal KV v2 reader with a short cache, so a sweep does not hit Vault for every probe."""

    def __init__(self, addr: str, token: str, mount: str = "secret", prefix: str = "labforge",
                 namespace: str | None = None, client: httpx.Client | None = None, ttl: float = 60):
        self.addr, self.token, self.mount, self.prefix = addr.rstrip("/"), token, mount.strip("/"), prefix.strip("/")
        self.namespace, self.client, self.ttl = namespace, client, ttl
        self._cache: dict[str, tuple[float, str | None]] = {}

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> VaultClient | None:
        addr, token = environ.get("VAULT_ADDR"), environ.get("VAULT_TOKEN")
        if not addr or not token:
            return None
        return cls(addr, token, environ.get("LABFORGE_VAULT_MOUNT", "secret"),
                   environ.get("LABFORGE_VAULT_PREFIX", "labforge"), environ.get("VAULT_NAMESPACE"))

    def path(self, ref: str) -> str:
        return f"{self.mount}/data/{self.prefix}/{ref}" if self.prefix else f"{self.mount}/data/{ref}"

    def read(self, ref: str) -> str | None:
        cached = self._cache.get(ref)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        headers = {"X-Vault-Token": self.token, **({"X-Vault-Namespace": self.namespace} if self.namespace else {})}
        try:
            client = self.client or httpx
            r = client.get(f"{self.addr}/v1/{self.path(ref)}", headers=headers, timeout=10)
            if r.status_code == 404:
                value = None
            else:
                r.raise_for_status()
                data = r.json()["data"]["data"]
                value = data.get("value", next(iter(data.values())) if len(data) == 1 else None)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("vault read of %s failed: %s", ref, e)
            return None
        self._cache[ref] = (time.monotonic(), value)
        return value


class SecretResolver:
    def __init__(self, dry_run: bool = False, environ: Mapping[str, str] | None = None,
                 vault: VaultClient | None = None):
        self.dry_run = dry_run
        self.environ = os.environ if environ is None else environ
        self.vault = vault if vault is not None else VaultClient.from_env(self.environ)

    def get_optional(self, ref: str | None) -> str | None:
        if not ref:
            return None
        value = self.environ.get(env_key(ref))
        if value is None and self.vault:
            value = self.vault.read(ref)
        return value

    def get(self, ref: str) -> str:
        value = self.get_optional(ref)
        if value is not None:
            return value
        if self.dry_run:
            return f"<secret:{ref}>"
        where = env_key(ref) + (f" or Vault {self.vault.path(ref)}" if self.vault else "")
        raise SecretError(f"missing secret {ref!r}: set {where}")

    def credentials(self, ref: str) -> tuple[str, str]:
        if self.dry_run and self.get_optional(ref) is None:
            return f"<user:{ref}>", f"<password:{ref}>"
        user, sep, password = self.get(ref).partition(":")
        if not sep and not self.dry_run:
            raise SecretError(f"secret {ref!r} must be in 'user:password' form")
        return user, password
