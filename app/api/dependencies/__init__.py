"""Request dependencies shared by all routers."""

from __future__ import annotations

from fastapi import Request

from app.core.context import AppContext
from app.core.policy.engine import Actor
from app.core.security.session import COOKIE_NAME
from app.services.documents.service import DocumentService
from app.services.system import SystemService


def get_context(request: Request) -> AppContext:
    return request.app.state.context


def get_documents(request: Request) -> DocumentService:
    return request.app.state.context.documents


def get_system(request: Request) -> SystemService:
    return request.app.state.system


def get_actor(request: Request) -> Actor:
    """Who is asking, and through which interface.

    A bearer token means a script or REST client; the session cookie means the
    browser UI. Destructive actions need the explicit confirmation header,
    which the UI sends only after the user confirms.
    """
    interface = "api" if request.headers.get("authorization") else "ui"
    if interface == "ui" and COOKIE_NAME not in request.cookies:
        interface = "api"
    approved = request.headers.get("x-ard-confirm", "").lower() == "yes"
    return Actor(name="local-user", interface=interface, approved=approved)
