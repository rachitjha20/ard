"""HTML pages. Jinja2 shell; all data arrives through the JSON API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.config.settings import APP_VERSION, FRONTEND_DIR

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))

# Decision D-020: Home, Library, Workspace, Intelligence, Automation, Trust & Settings.
NAVIGATION = [
    {"id": "home", "label": "Home", "href": "/", "icon": "home"},
    {"id": "library", "label": "Library", "href": "/library", "icon": "library"},
    {"id": "workspace", "label": "Workspace", "href": "/workspace", "icon": "workspace"},
    {"id": "intelligence", "label": "Intelligence", "href": "/intelligence", "icon": "intelligence"},
    {"id": "automation", "label": "Automation", "href": "/automation", "icon": "automation"},
    {"id": "trust", "label": "Trust & Settings", "href": "/trust", "icon": "trust"},
]


def _render(request: Request, template: str, page: str) -> HTMLResponse:
    context = request.app.state.context
    return templates.TemplateResponse(request, template, {
        "page": page,
        "navigation": NAVIGATION,
        "version": APP_VERSION,
        "safe_mode": context.policy.safe_mode,
        "safe_mode_reason": context.policy.safe_mode_reason,
    })


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return _render(request, "home.html", "home")


@router.get("/library", response_class=HTMLResponse)
def library(request: Request) -> HTMLResponse:
    return _render(request, "library.html", "library")


@router.get("/workspace", response_class=HTMLResponse)
def workspace(request: Request) -> HTMLResponse:
    return _render(request, "workspace.html", "workspace")


@router.get("/intelligence", response_class=HTMLResponse)
def intelligence(request: Request) -> HTMLResponse:
    return _render(request, "intelligence.html", "intelligence")


@router.get("/automation", response_class=HTMLResponse)
def automation(request: Request) -> HTMLResponse:
    return _render(request, "automation.html", "automation")


@router.get("/trust", response_class=HTMLResponse)
def trust(request: Request) -> HTMLResponse:
    return _render(request, "trust.html", "trust")
