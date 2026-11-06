"""CRA traceability in the web UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from labforge import cra
from labforge.api import get_session
from labforge.models import Sbom
from labforge.web import templates

router = APIRouter(prefix="/compliance", include_in_schema=False)


@router.get("", response_class=HTMLResponse)
def compliance_page(request: Request, component: str = "", below: str = "", session: Session = Depends(get_session)):
    result = cra.affected(session, component.strip(), below=below.strip() or None) if component.strip() else None
    inv = cra.inventory(session)
    return templates.TemplateResponse(request, "compliance.html", {
        "nav": "compliance", "inventory": inv, "sboms": list(session.exec(select(Sbom).order_by(Sbom.uploaded_at.desc()))),
        "q": {"component": component, "below": below}, "result": result,
        "report": cra.report_markdown(result) if result else "",
        "coverage": sum(1 for d in inv if d["sbom"]),
    })
