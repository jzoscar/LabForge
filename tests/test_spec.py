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
