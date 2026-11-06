"""IaC workspace in the web UI: browse, inspect, edit, import/export and apply."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from labforge import service
from labforge.api import get_session
from labforge.iac.inspect import describe, json_schema, schema_reference
from labforge.iac.loader import Diagnostic, LoadError, parse_text
from labforge.iac.planner import build_plan
from labforge.iac.workspace import Workspace, WorkspaceError
from labforge.models import ResourceState
from labforge.web import templates

router = APIRouter(prefix="/iac", include_in_schema=False)

STARTERS_DIR = Path(__file__).parent / "iac" / "starters"
STARTERS = {
    "blank": "Blank environment",
    "device-pool": "Device test rig pool (ARM sensors + BMC)",
    "build-farm": "Build farm (Linux Docker + Windows MSVC + caches)",
}


def _ws(request: Request) -> Workspace:
    return request.app.state.settings.workspace()


def _require_edit(request: Request) -> None:
    if not request.app.state.settings.ui_edit:
        raise HTTPException(403, "editing IaC from the web UI is disabled (LABFORGE_UI_EDIT=0)")


def _diag_json(d: Diagnostic) -> dict:
    return {"line": d.line, "message": d.message, "severity": d.severity}


# --- pages ------------------------------------------------------------------


TIER_ORDER = ["dev", "test", "rnd", "staging", "demo", "prod"]
GROUP_LABEL = "scope"  # metadata label used to filter environments


@router.get("", response_class=HTMLResponse)
def workspace(request: Request, tab: str = "all", env: str = "", label: str = "",
              session: Session = Depends(get_session)):
    return _render_workspace(request, session, tab=tab, env=env, label=label)


def _render_workspace(request: Request, session: Session, *, tab: str = "all", env: str = "", label: str = "",
                      import_results: list | None = None):
    """Environments are shown by tier (tabs) and scope label (filter); files and the
    workspace plan have their own tabs, so nothing is dumped onto one long page."""
    settings = request.app.state.settings
    ws = _ws(request)
    files = ws.files()

    plan, errors = None, []
    if files:
        try:
            _, plan = service.make_plan(session, ws.root)
        except LoadError as e:
            errors = [(d.file and _relative(ws, d.file), d) for d in e.diagnostics]
    pending: dict[str, dict[str, int]] = {}
    for ch in plan.changes if plan else []:
        if ch.action != "noop":
            counts = pending.setdefault(ch.env, {"create": 0, "update": 0, "delete": 0})
            counts[ch.action] += 1

    entries = []
    for f in files:
        docs, _ = parse_text(ws.read(f.path)[0], f.path)
        entries += [{"file": f.path, "view": v, "problems": len(f.diagnostics), "pending": pending.get(v.name)}
                    for v in describe(docs)]
    entries.sort(key=lambda e: (TIER_ORDER.index(e["view"].tier) if e["view"].tier in TIER_ORDER else 99,
                                e["view"].name))

    tiers = [(t, sum(1 for e in entries if e["view"].tier == t)) for t in TIER_ORDER]
    tiers = [(t, n) for t, n in tiers if n]
    scopes = sorted({e["view"].labels[GROUP_LABEL] for e in entries if GROUP_LABEL in e["view"].labels})

    shown = [e for e in entries if tab in ("all", e["view"].tier)]
    if label:
        shown = [e for e in shown if e["view"].labels.get(GROUP_LABEL) == label]
    selected = next((e for e in entries if e["view"].name == env), None)

    folders = _by_folder(files)

    if import_results is not None:
        tab = "files"
    return templates.TemplateResponse(request, "iac.html", {
        "nav": "iac", "root": ws.root, "tab": tab, "label": label, "selected": selected,
        "tiers": tiers, "scopes": scopes, "group_label": GROUP_LABEL,
        "entries": entries, "shown": shown, "files": files, "folders": folders,
        "plan": plan, "errors": errors,
        "resource_total": sum(f.resources for f in files), "problem_total": sum(len(f.diagnostics) for f in files),
        "ui_apply": settings.ui_apply, "ui_edit": settings.ui_edit, "starters": STARTERS,
        "import_results": import_results,
    })


def _by_folder(files: list) -> dict[str, list]:
    folders: dict[str, list] = {}
    for f in files:
        folders.setdefault(f.path.rsplit("/", 1)[0] if "/" in f.path else "", []).append(f)
    # Folders named after a tier follow the tier order (dev, test, staging, demo...).
    def key(item: tuple[str, list]) -> tuple[int, str]:
        top = item[0].split("/", 1)[0]
        return (TIER_ORDER.index(top) if top in TIER_ORDER else len(TIER_ORDER), item[0])
    return dict(sorted(folders.items(), key=key))


def _relative(ws: Workspace, file: str) -> str:
    try:
        return ws.rel(Path(file))
    except ValueError:
        return file


@router.get("/edit", response_class=HTMLResponse)
def editor(request: Request, path: str):
    ws = _ws(request)
    try:
        text, digest = ws.read(path)
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    settings = request.app.state.settings
    return templates.TemplateResponse(request, "editor.html", {
        "nav": "iac", "path": path, "text": text, "digest": digest, "folders": _by_folder(ws.files()),
        "ui_edit": settings.ui_edit, "ui_apply": settings.ui_apply,
    })


@router.get("/schema", response_class=HTMLResponse)
def schema_page(request: Request):
    return templates.TemplateResponse(request, "schema.html", {"nav": "iac", "sections": schema_reference()})


@router.get("/schema.json")
def schema_json() -> JSONResponse:
    return JSONResponse(json_schema())


# --- editor endpoints (JSON) ------------------------------------------------


class CheckRequest(BaseModel):
    path: str
    text: str


class SaveRequest(CheckRequest):
    base: str | None = None


@router.post("/check")
def check(req: CheckRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    """Validate editor contents (unsaved) and preview the structure and plan they would produce."""
    ws = _ws(request)
    docs, diags = parse_text(req.text, req.path)
    owners = ws.env_owners(exclude=req.path)
    for doc in docs:
        if doc.metadata.name in owners:
            line = next((i + 1 for i, ln in enumerate(req.text.splitlines())
                         if re.search(rf"\bname:\s*['\"]?{re.escape(doc.metadata.name)}\b", ln)), None)
            diags.append(Diagnostic(req.path, line,
                                    f"environment {doc.metadata.name!r} is already defined in {owners[doc.metadata.name]}"))
    plan = None
    if not diags and docs:
        p = build_plan(docs, list(session.exec(select(ResourceState))))
        plan = {"counts": p.counts(),
                "changes": [{"action": c.action, "address": c.address, "diff": c.diff} for c in p.changes]}
    return {
        "diagnostics": [_diag_json(d) for d in sorted(diags, key=lambda d: d.line or 0)],
        "envs": [{"name": e.name, "tier": e.tier, "mermaid": e.mermaid,
                  "resources": [{"kind": r.kind, "name": r.name, "summary": r.summary, "dependsOn": r.depends_on}
                                for r in e.resources]} for e in describe(docs)],
        "plan": plan,
    }


@router.post("/save")
def save(req: SaveRequest, request: Request) -> JSONResponse:
    _require_edit(request)
    try:
        digest = _ws(request).write(req.path, req.text, req.base)
    except WorkspaceError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    _, diags = parse_text(req.text, req.path)
    return JSONResponse({"digest": digest, "problems": len(diags)})


@router.get("/history")
def history(request: Request, path: str) -> JSONResponse:
    try:
        revs = _ws(request).history(path)
    except WorkspaceError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse([{"id": r["id"], "ts": r["ts"].isoformat(), "size": r["size"]} for r in revs])


@router.get("/revision")
def revision(request: Request, path: str, id: str) -> JSONResponse:
    try:
        return JSONResponse({"text": _ws(request).revision(path, id)})
    except WorkspaceError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


# --- file management (forms) ------------------------------------------------


@router.post("/new")
def new_file(request: Request, name: str = Form(...), env: str = Form(""), starter: str = Form("blank")):
    _require_edit(request)
    if starter not in STARTERS:
        raise HTTPException(400, "unknown starter")
    rel = name.strip()
    if not rel.endswith((".yaml", ".yml")):
        rel += ".yaml"
    env_name = env.strip() or re.sub(r"[^a-z0-9-]", "-", Path(rel).stem.lower()).strip("-") or "new-env"
    text = (STARTERS_DIR / f"{starter}.yaml").read_text().replace("__ENV__", env_name)
    try:
        _ws(request).write(rel, text, create=True)
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    return RedirectResponse(f"/iac/edit?path={rel}", status_code=303)


@router.post("/import", response_class=HTMLResponse)
async def import_files(request: Request, files: list[UploadFile] = File(...), overwrite: bool = Form(False),
                       session: Session = Depends(get_session)):
    _require_edit(request)
    ws = _ws(request)
    results = []
    for upload in files:
        results += ws.import_upload(upload.filename or "upload.yaml", await upload.read(), overwrite)
    return _render_workspace(request, session, import_results=results)


@router.get("/export")
def export(request: Request) -> Response:
    data = _ws(request).export_zip()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="labforge-iac.zip"'})


@router.get("/download")
def download(request: Request, path: str) -> Response:
    try:
        text, _ = _ws(request).read(path)
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    return Response(text, media_type="application/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{Path(path).name}"'})


@router.post("/delete")
def delete(request: Request, path: str = Form(...)):
    _require_edit(request)
    try:
        _ws(request).delete(path)
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    return RedirectResponse("/iac", status_code=303)


@router.post("/apply")
def apply(request: Request, dry_run: bool = Form(False), allow_prod: bool = Form(False), path: str = Form("")):
    settings = request.app.state.settings
    if not dry_run and not settings.ui_apply:
        raise HTTPException(403, "real apply from the UI is disabled; set LABFORGE_UI_APPLY=1 or use the CLI")
    ws = _ws(request)
    try:
        target = ws.resolve(path) if path else ws.root
    except WorkspaceError as e:
        raise HTTPException(e.status, str(e)) from e
    run_id = service.start_run_background(request.app.state.engine, settings, target, dry_run=dry_run,
                                          allow_prod=allow_prod, source=f"web:{path or 'workspace'}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)
