"""EU Cyber Resilience Act traceability: which firmware runs where, what is inside it, and which
devices a newly published vulnerability reaches.

- Inventory: every device's firmware, recorded when LabForge flashes it and whenever monitoring
  reads its version endpoint (a JSON `firmware` field).
- SBOMs: one per firmware version, uploaded from the build (BuildPulse `scan` writes CycloneDX and SPDX).
- Impact: for a component and an affected version range, the firmware versions that contain it, the
  devices running them now, and the devices whose firmware has no SBOM (a gap to close).
"""

from __future__ import annotations

import json
import re

from sqlmodel import Session, select

from labforge.models import FirmwareSeen, ResourceState, Sbom, utcnow


def record_firmware(session: Session, address: str, version: str, sha256: str | None, source: str) -> None:
    row = session.exec(select(FirmwareSeen).where(FirmwareSeen.address == address,
                                                  FirmwareSeen.version == version)).first()
    if row is None:
        row = FirmwareSeen(address=address, version=version, sha256=sha256, source=source)
    row.last_seen = utcnow()
    if sha256:
        row.sha256 = sha256
    if source == "flash":
        row.source = "flash"
    session.add(row)


def inventory(session: Session) -> list[dict]:
    """The firmware each device ran most recently."""
    latest: dict[str, FirmwareSeen] = {}
    for row in session.exec(select(FirmwareSeen).order_by(FirmwareSeen.last_seen)):
        latest[row.address] = row
    states = {s.address: s for s in session.exec(select(ResourceState))}
    sboms = {s.version for s in session.exec(select(Sbom))}
    return [{"address": a, "env": states[a].env if a in states else "", "version": r.version, "sha256": r.sha256,
             "source": r.source, "lastSeen": r.last_seen, "sbom": r.version in sboms}
            for a, r in sorted(latest.items())]


def parse_sbom(doc: dict) -> tuple[str, list[dict]]:
    if doc.get("bomFormat") == "CycloneDX" or "components" in doc:
        comps = [{"name": c.get("name", ""), "version": c.get("version", ""), "purl": c.get("purl", "")}
                 for c in doc.get("components", [])]
        return "cyclonedx", comps
    if "spdxVersion" in doc or "packages" in doc:
        comps = []
        for p in doc.get("packages", []):
            purl = next((r.get("referenceLocator", "") for r in p.get("externalRefs", [])
                         if r.get("referenceType") == "purl"), "")
            comps.append({"name": p.get("name", ""), "version": p.get("versionInfo", ""), "purl": purl})
        return "spdx", comps
    raise ValueError("not a CycloneDX or SPDX JSON document")


def store_sbom(session: Session, version: str, doc: dict) -> Sbom:
    fmt, comps = parse_sbom(doc)
    row = session.exec(select(Sbom).where(Sbom.version == version)).first() or Sbom(version=version)
    row.format, row.components_json, row.component_count, row.uploaded_at = fmt, json.dumps(comps), len(comps), utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def version_key(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (v or "",)


def _component_matches(c: dict, name: str) -> bool:
    name = name.lower()
    return c["name"].lower() == name or f"/{name}@" in c.get("purl", "").lower()


def affected(session: Session, component: str, *, below: str | None = None,
             versions: list[str] | None = None) -> dict:
    """Firmware versions containing a vulnerable component, and the devices running them now."""

    def vulnerable(v: str) -> bool:
        if versions:
            return v in versions
        if below:
            return version_key(v) < version_key(below)
        return True

    firmware = []
    for sbom in session.exec(select(Sbom)):
        hits = [c for c in json.loads(sbom.components_json) if _component_matches(c, component) and vulnerable(c["version"])]
        if hits:
            firmware.append({"firmware": sbom.version, "componentVersions": sorted({c["version"] for c in hits})})
    bad = {f["firmware"] for f in firmware}
    inv = inventory(session)
    return {
        "component": component, "below": below, "versions": versions, "checkedAt": utcnow(),
        "firmware": sorted(firmware, key=lambda f: version_key(f["firmware"])),
        "devices": [d for d in inv if d["version"] in bad],
        "unknown": [d for d in inv if not d["sbom"]],  # running firmware nobody can vouch for
    }


def report_markdown(result: dict) -> str:
    rng = f"< {result['below']}" if result["below"] else (", ".join(result["versions"] or []) or "any version")
    lines = [f"# Exposure: {result['component']} {rng}", "", f"Checked {result['checkedAt']:%Y-%m-%d %H:%M} UTC.", ""]
    lines += ["## Affected firmware", ""] + ([f"- {f['firmware']} (contains {', '.join(f['componentVersions'])})"
                                             for f in result["firmware"]] or ["- none"])
    lines += ["", "## Devices running affected firmware", ""] + ([f"- {d['address']} ({d['env']}): {d['version']}"
                                                                  for d in result["devices"]] or ["- none"])
    if result["unknown"]:
        lines += ["", "## Devices without an SBOM for their firmware (cannot be ruled out)", ""]
        lines += [f"- {d['address']}: {d['version']}" for d in result["unknown"]]
    return "\n".join(lines) + "\n"
