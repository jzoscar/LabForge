from __future__ import annotations

import pytest

from conftest import ROOT, write_env
from labforge.iac.loader import LoadError, interpolate, load_path

HEADER = "apiVersion: labforge/v1\nkind: Environment\nmetadata: {name: t, tier: test}\n"


def test_example_infra_is_valid():
    names = {d.metadata.name for d in load_path(ROOT / "infra")}
    assert names == {"dev", "test-unit", "test-integration", "sim-lab", "staging", "demo"}
    assert {d.metadata.name for d in load_path(ROOT / "examples" / "local")} == {"local"}


def test_interpolation():
    text, missing = interpolate("a=${A} b=${B:-dflt} c=${C}", {"A": "1"})
    assert text == "a=1 b=dflt c=${C}"
    assert missing == ["C"]


@pytest.mark.parametrize(
    "spec,needle",
    [
        ("networks: [{name: n, cidr: 10.0.0.0/16, subnets: [{name: s, cidr: 10.1.0.0/24}]}]", "outside 10.0.0.0/16"),
        ("networks: [{name: n, cidr: 10.0.0.0/24, subnets: [{name: s, cidr: 10.0.0.0/25, gateway: 10.0.0.200}]}]",
         "gateway"),
        ("containers: [{name: c, host: ghost, image: x}]", "unknown host 'ghost'"),
        ("hosts: [{name: h, platform: {os: linux}, power: 'on'}]", "power requires a bmc"),
        ("hosts: [{name: h, platform: {os: linux}, bootstrap: [ls]}]", "bootstrap requires address"),
        ("hosts: [{name: h, platform: {os: linux}, adress: 1.2.3.4}]", "Extra inputs are not permitted"),
        ("hosts: [{name: h, platform: {os: linux}}, {name: h, platform: {os: linux}}]", "duplicate hosts"),
        ("hosts: [{name: w, platform: {os: windows}, connection: {type: winrm}}]", "passwordRef"),
        ("loadBalancers: [{name: lb, listeners: [{port: 80}], targets: [x]}]", "unknown target host"),
        ("loadBalancers: [{name: lb, listeners: [{port: 80}], driver: haproxy}]", "requires host"),
        ("networks: [{name: n, cidr: 10.0.0.0/24, subnets: [{name: s, cidr: 10.0.0.0/24}]}]\n"
         "  hosts: [{name: h, platform: {os: linux}, subnet: n/s, address: 10.9.9.9}]", "outside subnet"),
    ],
)
def test_validation_errors(tmp_path, spec, needle):
    f = write_env(tmp_path / "e.yaml", HEADER + "spec:\n  " + spec + "\n")
    with pytest.raises(LoadError) as e:
        load_path(f)
    assert needle in "\n".join(e.value.errors)


def test_duplicate_env_across_files(tmp_path):
    write_env(tmp_path / "a.yaml", HEADER)
    write_env(tmp_path / "b.yaml", HEADER)
    with pytest.raises(LoadError, match="already defined"):
        load_path(tmp_path)


def test_additive_migration_adds_new_nullable_columns(tmp_path):
    import sqlite3

    from labforge.db import make_engine

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE lease (id INTEGER PRIMARY KEY, address VARCHAR, pool VARCHAR, holder VARCHAR,"
                " purpose VARCHAR, status VARCHAR, result VARCHAR, acquired_at DATETIME, expires_at DATETIME,"
                " released_at DATETIME, note VARCHAR)")
    con.execute("INSERT INTO lease (address, pool, holder, status) VALUES ('a', 'p', 'h', 'active')")
    con.commit()
    con.close()
    make_engine(f"sqlite:///{db}")
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(lease)")}
    assert {"address", "note"} <= cols  # existing data untouched, nothing dropped


def test_vault_backend_after_environment():
    import httpx
    import pytest

    from labforge.iac.secrets import SecretError, SecretResolver, VaultClient

    seen = []

    def vault(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["X-Vault-Token"]))
        if request.url.path == "/v1/secret/data/labforge/bmc-default":
            return httpx.Response(200, json={"data": {"data": {"value": "admin:vaulted"}}})
        return httpx.Response(404, json={"errors": []})

    client = VaultClient("https://vault.test", "s.tok", client=httpx.Client(transport=httpx.MockTransport(vault)))
    r = SecretResolver(environ={"LABFORGE_SECRET_LOCAL": "from-env"}, vault=client)
    assert r.get("local") == "from-env" and seen == []  # the environment wins, Vault not asked
    assert r.credentials("bmc-default") == ("admin", "vaulted")
    r.get("bmc-default")
    assert seen == [("/v1/secret/data/labforge/bmc-default", "s.tok")]  # cached
    with pytest.raises(SecretError, match="Vault secret/data/labforge/missing"):
        r.get("missing")
    assert VaultClient.from_env({"VAULT_ADDR": "https://v"}) is None  # no token, no Vault
