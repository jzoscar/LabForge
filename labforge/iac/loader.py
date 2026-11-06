"""Load IaC YAML from a file or directory, with ${VAR} / ${VAR:-default} interpolation.

Every problem is reported as a Diagnostic carrying the file and, where it can be
resolved from the YAML node tree, the line number - so editors can mark it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from labforge.iac.spec import AlertPolicyDoc, EnvironmentDoc

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# Cross-reference errors name the offending resource: "host arm1: unknown subnet ..."
_SUBJECT = re.compile(r"^(?:network|loadBalancer|host|container) ([a-z0-9-]+):")


@dataclass
class Diagnostic:
    file: str
    line: int | None
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}" if self.line else f"{self.file}: {self.message}"


class LoadError(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        self.errors = [str(d) for d in diagnostics]
        super().__init__("\n".join(self.errors))


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
    return sorted(f for f in path.rglob("*") if f.suffix in {".yaml", ".yml"} and f.is_file()
                  and not any(p.startswith(".") for p in f.relative_to(path).parts))


def _node_line(node: yaml.Node, loc: tuple) -> int:
    """Walk a pydantic error location into the YAML node tree; return the deepest line found."""
    for key in loc:
        if isinstance(node, yaml.MappingNode):
            match = next((v for k, v in node.value if k.value == str(key)), None)
            if match is None:
                break
            node = match
        elif isinstance(node, yaml.SequenceNode) and isinstance(key, int) and key < len(node.value):
            node = node.value[key]
        else:
            break
    return node.start_mark.line + 1


def _name_line(node: yaml.Node, name: str) -> int | None:
    for n in _walk(node):
        if isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                if k.value == "name" and getattr(v, "value", None) == name:
                    return n.start_mark.line + 1
    return None


def _walk(node: yaml.Node):
    yield node
    if isinstance(node, yaml.MappingNode):
        for _, v in node.value:
            yield from _walk(v)
    elif isinstance(node, yaml.SequenceNode):
        for v in node.value:
            yield from _walk(v)


def _messages(err: dict) -> list[str]:
    # Errors raised in model validators join several problems with "; ".
    msg = err["msg"].removeprefix("Value error, ")
    return [m for m in msg.split("; ") if m] if err["type"] == "value_error" else [msg]


KINDS = {"Environment": EnvironmentDoc, "AlertPolicy": AlertPolicyDoc}


def parse_text(text: str, source: str = "<buffer>",
               environ: Mapping[str, str] | None = None) -> tuple[list[EnvironmentDoc], list[Diagnostic]]:
    """Parse one YAML file's text into environment documents plus diagnostics (all kinds are validated)."""
    docs, diags = parse_documents(text, source, environ)
    return [d for d in docs if isinstance(d, EnvironmentDoc)], diags


def parse_documents(text: str, source: str = "<buffer>", environ: Mapping[str, str] | None = None
                    ) -> tuple[list[EnvironmentDoc | AlertPolicyDoc], list[Diagnostic]]:
    """Parse one YAML file into documents of every kind (Environment, AlertPolicy)."""
    environ = os.environ if environ is None else environ
    text, missing = interpolate(text, environ)
    diags: list[Diagnostic] = []
    for name in missing:
        line = next((i + 1 for i, ln in enumerate(text.splitlines()) if "${" + name in ln), None)
        diags.append(Diagnostic(source, line, f"undefined variable ${{{name}}}"))
    try:
        nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        raws = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = getattr(e, "problem", None) or str(e)
        return [], diags + [Diagnostic(source, mark.line + 1 if mark else None, f"invalid YAML: {problem}")]

    docs: list[EnvironmentDoc | AlertPolicyDoc] = []
    for node, raw in zip(nodes, raws):
        if raw is None or node is None:
            continue
        model = KINDS.get(raw.get("kind")) if isinstance(raw, dict) else None
        if model is None:
            diags.append(Diagnostic(source, node.start_mark.line + 1,
                                    f"kind: expected one of {', '.join(KINDS)}, got {raw.get('kind') if isinstance(raw, dict) else raw!r}"))
            continue
        try:
            doc = model.model_validate(raw)
            if isinstance(doc, EnvironmentDoc):
                doc._source = source
            docs.append(doc)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"])
                line = _node_line(node, err["loc"])
                for msg in _messages(err):
                    if not err["loc"] and (m := _SUBJECT.match(msg)):
                        line = _name_line(node, m.group(1)) or line
                    diags.append(Diagnostic(source, line, f"{loc + ': ' if loc else ''}{msg}"))
    return docs, diags


def load_path(path: str | Path, environ: Mapping[str, str] | None = None) -> list[EnvironmentDoc]:
    path = Path(path)
    if not path.exists():
        raise LoadError([Diagnostic(str(path), None, "not found")])
    files = iac_files(path)
    if not files:
        raise LoadError([Diagnostic(str(path), None, "no .yaml/.yml files")])

    docs: list[EnvironmentDoc] = []
    diags: list[Diagnostic] = []
    origin: dict[str, str] = {}
    for f in files:
        file_docs, file_diags = parse_text(f.read_text(encoding="utf-8"), str(f), environ)
        diags += file_diags
        for doc in file_docs:
            name = doc.metadata.name
            if name in origin:
                diags.append(Diagnostic(str(f), None, f"environment {name!r} already defined in {origin[name]}"))
                continue
            origin[name] = str(f)
            docs.append(doc)
    if diags:
        raise LoadError(diags)
    return docs


def load_policies(path: str | Path, environ: Mapping[str, str] | None = None) -> list[AlertPolicyDoc]:
    """AlertPolicy documents in a workspace; invalid files are skipped (they show up as problems)."""
    path = Path(path)
    out: list[AlertPolicyDoc] = []
    for f in iac_files(path) if path.exists() else []:
        docs, _ = parse_documents(f.read_text(encoding="utf-8"), str(f), environ)
        out += [d for d in docs if isinstance(d, AlertPolicyDoc)]
    return out
