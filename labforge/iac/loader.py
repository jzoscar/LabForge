"""Load IaC YAML from a file or directory, with ${VAR} / ${VAR:-default} interpolation."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from labforge.iac.spec import EnvironmentDoc

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class LoadError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def interpolate(text: str, environ: Mapping[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in environ:
            return environ[name]
        if default is not None:
            return default
        missing.append(name)
        return m.group(0)

    return _VAR.sub(sub, text), missing


def iac_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(f for f in path.rglob("*") if f.suffix in {".yaml", ".yml"} and f.is_file())


def load_path(path: str | Path, environ: Mapping[str, str] | None = None) -> list[EnvironmentDoc]:
    path = Path(path)
    environ = os.environ if environ is None else environ
    if not path.exists():
        raise LoadError([f"{path}: not found"])
    files = iac_files(path)
    if not files:
        raise LoadError([f"{path}: no .yaml/.yml files"])

    docs: list[EnvironmentDoc] = []
    errors: list[str] = []
    origin: dict[str, str] = {}
    for f in files:
        text, missing = interpolate(f.read_text(encoding="utf-8"), environ)
        errors += [f"{f}: undefined variable ${{{name}}}" for name in missing]
        try:
            raws = [r for r in yaml.safe_load_all(text) if r is not None]
        except yaml.YAMLError as e:
            errors.append(f"{f}: invalid YAML: {e}")
            continue
        for i, raw in enumerate(raws):
            where = f"{f}" if len(raws) == 1 else f"{f}#{i}"
            try:
                doc = EnvironmentDoc.model_validate(raw)
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(x) for x in err["loc"])
                    errors.append(f"{where}: {loc + ': ' if loc else ''}{err['msg']}")
                continue
            name = doc.metadata.name
            if name in origin:
                errors.append(f"{where}: environment {name!r} already defined in {origin[name]}")
                continue
            origin[name] = where
            docs.append(doc)
    if errors:
        raise LoadError(errors)
    return docs
