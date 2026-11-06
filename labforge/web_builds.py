"""Build-farm speed in the web UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from labforge import builds
from labforge.api import get_session
from labforge.models import BuildRun
from labforge.web import sparkline, templates

router = APIRouter(prefix="/builds", include_in_schema=False)


@router.get("", response_class=HTMLResponse)
def builds_page(request: Request, days: int = 30, session: Session = Depends(get_session)):
    pipelines = builds.stats(session, min(max(days, 1), 365))
    for p in pipelines:
        p["spark"] = sparkline(p["trend"], vmax=max(p["trend"] or [1]) * 1.1)
    runs = sum(p["runs"] for p in pipelines)
    failed = sum(p["failed"] for p in pipelines)
    return templates.TemplateResponse(request, "builds.html", {
        "nav": "builds", "days": days, "pipelines": pipelines, "suggestions": builds.suggestions(pipelines),
        "runs": runs, "success": (runs - failed) / runs if runs else None,
        "recent": list(session.exec(select(BuildRun).order_by(BuildRun.id.desc()).limit(20))),
    })
