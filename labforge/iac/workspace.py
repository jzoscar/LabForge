"""The IaC workspace: the folder of YAML files that declares the lab.

All file access from the web UI and API goes through here, so path handling,
conflict detection and revision history live in one place.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from labforge.iac.loader import Diagnostic, iac_files, parse_text
from labforge.iac.planner import flatten

SUFFIXES = {".yaml", ".yml"}
_SAFE_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
MAX_FILE_BYTES = 1_000_000
MAX_IMPORT_BYTES = 20_000_000


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class FileInfo:
    path: str
    size: int
    modified: datetime
    digest: str
    envs: list[str] = field(default_factory=list)
    resources: int = 0
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class ImportResult:
    path: str
    status: str  # created | updated | skipped | rejected
    detail: str = ""


class Workspace:
    def __init__(self, root: Path, history_dir: Path):
        self.root = root.resolve()
        self.history_dir = history_dir

    # --- paths --------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Map a workspace-relative path to a file inside the workspace, or refuse."""
        pure = PurePosixPath(rel.replace("\\", "/").strip())
        if pure.is_absolute() or not pure.parts:
            raise WorkspaceError(f"invalid path {rel!r}")
        if any(not _SAFE_PART.match(p) for p in pure.parts):
            raise WorkspaceError(f"invalid path {rel!r}: use letters, digits, '-', '_' and '.' only")
        if pure.suffix not in SUFFIXES:
            raise WorkspaceError(f"invalid path {rel!r}: only .yaml / .yml files are managed")
        path = (self.root / Path(*pure.parts)).resolve()
        if not path.is_relative_to(self.root):
            raise WorkspaceError(f"invalid path {rel!r}")
        return path

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    # --- reading ------------------------------------------------------------

    def files(self) -> list[FileInfo]:
        if not self.root.is_dir():
            return []
        out = []
        for f in iac_files(self.root):
            data = f.read_bytes()
            docs, diags = parse_text(data.decode("utf-8", errors="replace"), self.rel(f))
            st = f.stat()
            out.append(FileInfo(
                path=self.rel(f), size=st.st_size, modified=datetime.fromtimestamp(st.st_mtime, UTC),
                digest=digest(data), envs=[d.metadata.name for d in docs],
                resources=sum(len(flatten(d)) for d in docs), diagnostics=diags,
            ))
        return out

    def read(self, rel: str) -> tuple[str, str]:
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError(f"{rel} not found", 404)
        data = path.read_bytes()
        return data.decode("utf-8"), digest(data)

    def env_owners(self, exclude: str | None = None) -> dict[str, str]:
        """environment name -> file that declares it (for duplicate detection while editing)."""
        owners: dict[str, str] = {}
        for info in self.files():
            if info.path != exclude:
                for env in info.envs:
                    owners.setdefault(env, info.path)
        return owners

    # --- writing ------------------------------------------------------------

    def write(self, rel: str, text: str, base_digest: str | None = None, *, create: bool = False) -> str:
        """Save a file. ``base_digest`` is the version the editor started from; if the file
        changed on disk since then the save is refused instead of silently overwriting."""
        path = self.resolve(rel)
        data = text.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise WorkspaceError(f"{rel} is larger than {MAX_FILE_BYTES // 1000} kB")
        if path.exists():
            if create:
                raise WorkspaceError(f"{rel} already exists", 409)
            current = path.read_bytes()
            if base_digest and digest(current) != base_digest:
                raise WorkspaceError(f"{rel} was changed by someone else; reload before saving", 409)
            if current == data:
                return digest(data)
            self._snapshot(rel, current)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return digest(data)

    def delete(self, rel: str) -> None:
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError(f"{rel} not found", 404)
        self._snapshot(rel, path.read_bytes())
        path.unlink()

    # --- history ------------------------------------------------------------

    def _history_path(self, rel: str) -> Path:
        return self.history_dir / PurePosixPath(rel)

    def _snapshot(self, rel: str, data: bytes) -> None:
        d = self._history_path(rel)
        d.mkdir(parents=True, exist_ok=True)
        (d / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".yaml")).write_bytes(data)

    def history(self, rel: str) -> list[dict]:
        self.resolve(rel)
        d = self._history_path(rel)
        if not d.is_dir():
            return []
        revs = sorted(d.glob("*.yaml"), reverse=True)
        return [{"id": r.stem, "ts": datetime.strptime(r.stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC),
                 "size": r.stat().st_size} for r in revs]

    def revision(self, rel: str, rev_id: str) -> str:
        self.resolve(rel)
        if not re.fullmatch(r"\d{8}T\d{12}Z", rev_id):
            raise WorkspaceError("invalid revision id")
        path = self._history_path(rel) / f"{rev_id}.yaml"
        if not path.is_file():
            raise WorkspaceError("revision not found", 404)
        return path.read_text(encoding="utf-8")

    # --- import / export ----------------------------------------------------

    def export_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in iac_files(self.root) if self.root.is_dir() else []:
                z.write(f, self.rel(f))
        return buf.getvalue()

    def import_upload(self, filename: str, data: bytes, overwrite: bool = False) -> list[ImportResult]:
        if len(data) > MAX_IMPORT_BYTES:
            return [ImportResult(filename, "rejected", "upload too large")]
        if filename.lower().endswith(".zip"):
            try:
                entries = self._zip_entries(data)
            except zipfile.BadZipFile:
                return [ImportResult(filename, "rejected", "not a valid zip file")]
        else:
            entries = [(PurePosixPath(filename.replace("\\", "/")).name, data)]
        return [self._import_one(name, content, overwrite) for name, content in entries]

    @staticmethod
    def _zip_entries(data: bytes) -> list[tuple[str, bytes]]:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and PurePosixPath(i.filename).suffix in SUFFIXES
                     and not any(p.startswith((".", "__")) for p in PurePosixPath(i.filename).parts)]
            total = sum(i.file_size for i in infos)
            if total > MAX_IMPORT_BYTES:
                raise zipfile.BadZipFile("archive too large")
            names = [PurePosixPath(i.filename) for i in infos]
            # A zipped folder ("infra/dev.yaml", ...) imports its contents, not the folder.
            tops = {n.parts[0] for n in names if len(n.parts) > 1}
            strip = len(tops) == 1 and all(len(n.parts) > 1 for n in names)
            return [(PurePosixPath(*n.parts[1:]).as_posix() if strip else n.as_posix(), z.read(i))
                    for n, i in zip(names, infos)]

    def _import_one(self, rel: str, content: bytes, overwrite: bool) -> ImportResult:
        try:
            path = self.resolve(rel)
            text = content.decode("utf-8")
        except WorkspaceError as e:
            return ImportResult(rel, "rejected", str(e))
        except UnicodeDecodeError:
            return ImportResult(rel, "rejected", "not UTF-8 text")
        existed = path.exists()
        if existed and not overwrite:
            return ImportResult(rel, "skipped", "already exists (tick overwrite to replace)")
        try:
            self.write(rel, text)
        except WorkspaceError as e:
            return ImportResult(rel, "rejected", str(e))
        _, diags = parse_text(text, rel)
        detail = f"{len(diags)} problem(s) to fix" if diags else "valid"
        return ImportResult(rel, "updated" if existed else "created", detail)
