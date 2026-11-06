from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from labforge import cra
from labforge.main import create_app
from labforge.models import ResourceState

CDX = {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
    {"name": "zlib", "version": "1.2.13", "purl": "pkg:conan/zlib@1.2.13"},
    {"name": "gtest", "version": "1.14.0", "purl": "pkg:conan/gtest@1.14.0"}]}
CDX_FIXED = {"bomFormat": "CycloneDX", "components": [{"name": "zlib", "version": "1.3.1", "purl": "pkg:conan/zlib@1.3.1"}]}
SPDX = {"spdxVersion": "SPDX-2.3", "packages": [
    {"name": "openssl", "versionInfo": "3.0.2",
     "externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:generic/openssl@3.0.2"}]}]}


def _device(session, name, env="sim-lab"):
    session.add(ResourceState(address=f"{env}.host.{name}", env=env, tier="test", kind="host", name=name,
                              spec_json="{}", spec_hash="x"))


def test_parse_both_formats():
    assert cra.parse_sbom(CDX)[0] == "cyclonedx" and cra.parse_sbom(CDX)[1][0]["purl"] == "pkg:conan/zlib@1.2.13"
    fmt, comps = cra.parse_sbom(SPDX)
    assert fmt == "spdx" and comps == [{"name": "openssl", "version": "3.0.2", "purl": "pkg:generic/openssl@3.0.2"}]
    with pytest.raises(ValueError):
        cra.parse_sbom({"hello": 1})
    assert cra.version_key("1.2.13") < cra.version_key("1.3.1") < cra.version_key("1.10")


def test_affected_devices_and_gaps(session):
    for n in ("a", "b", "c"):
        _device(session, n)
    cra.store_sbom(session, "2.0.0", CDX)
    cra.store_sbom(session, "2.1.0", CDX_FIXED)
    cra.record_firmware(session, "sim-lab.host.a", "2.0.0", "aa", "flash")
    cra.record_firmware(session, "sim-lab.host.b", "2.1.0", None, "probe")
    cra.record_firmware(session, "sim-lab.host.c", "1.9.9", None, "probe")  # no SBOM for this one
    session.commit()

    r = cra.affected(session, "zlib", below="1.3.1")
    assert r["firmware"] == [{"firmware": "2.0.0", "componentVersions": ["1.2.13"]}]
    assert [d["address"] for d in r["devices"]] == ["sim-lab.host.a"]
    assert [d["address"] for d in r["unknown"]] == ["sim-lab.host.c"]
    assert cra.affected(session, "zlib", versions=["1.3.1"])["devices"][0]["address"] == "sim-lab.host.b"
    md = cra.report_markdown(r)
    assert "# Exposure: zlib < 1.3.1" in md and "- sim-lab.host.a (sim-lab): 2.0.0" in md and "cannot be ruled out" in md

    cra.record_firmware(session, "sim-lab.host.a", "2.1.0", None, "flash")  # updated in the field
    session.commit()
    assert cra.affected(session, "zlib", below="1.3.1")["devices"] == []


def test_cra_api_and_page(settings, engine):
    with Session(engine) as s:
        _device(s, "a")
        cra.record_firmware(s, "sim-lab.host.a", "2.0.0", None, "probe")
        s.commit()
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/v1/firmware/2.0.0/sbom", json={"x": 1}).status_code == 422
        assert c.post("/api/v1/firmware/2.0.0/sbom", json=CDX).json() == {"version": "2.0.0", "format": "cyclonedx",
                                                                          "components": 2}
        assert c.get("/api/v1/cra/inventory").json()[0]["sbom"] is True
        assert c.get("/api/v1/cra/affected", params={"component": "zlib", "below": "1.3"}).json()["devices"]
        md = c.get("/api/v1/cra/affected", params={"component": "zlib", "below": "1.3", "format": "markdown"})
        assert md.headers["content-type"].startswith("text/markdown") and "sim-lab.host.a" in md.text
        page = c.get("/compliance", params={"component": "zlib", "below": "1.3.1"}).text
        assert "Devices running it now" in page and "sim-lab.host.a" in page
