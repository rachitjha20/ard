"""Local session security.

ARD listens on loopback only. On top of that:

* A random token is generated per installation and stored in the data
  directory with owner-only permissions.
* The browser receives it as an HttpOnly, SameSite=Strict cookie when it loads
  a page. Other tools (CLI, scripts) send it as a bearer token.
* The Host header must name a loopback address, which blocks DNS-rebinding.
* Requests that change state must not come from a foreign Origin.

Limitation: another user account on the same machine can reach loopback and
load a page to obtain a cookie. A stronger local authentication model is an
open decision in the decision log.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

COOKIE_NAME = "ard_session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def load_or_create_token(path: Path) -> str:
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def is_loopback_host(value: str) -> bool:
    return _hostname(value) in LOOPBACK_HOSTS


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self._token = token

    def _authenticated(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE_NAME)
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, self._token)

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        if not is_loopback_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "ARD only answers requests addressed to this computer."}, status_code=403)

        origin = request.headers.get("origin")
        if origin and request.method not in SAFE_METHODS:
            parts = urlsplit(origin)
            if parts.netloc.lower() != request.headers.get("host", "").lower():
                return JSONResponse({"detail": "Cross-origin requests are not accepted."}, status_code=403)

        path = request.url.path
        is_api = path.startswith("/api/")
        if is_api and path != "/api/health" and not self._authenticated(request):
            return JSONResponse({"detail": "Not authenticated. Open ARD in the browser or send the session token."}, status_code=401)

        response = await call_next(request)

        # Pages hand the session cookie to the browser that loaded them.
        if not is_api and not path.startswith("/static/") and request.method == "GET":
            if request.cookies.get(COOKIE_NAME) != self._token:
                response.set_cookie(COOKIE_NAME, self._token, httponly=True, samesite="strict", path="/")

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not path.startswith("/static/"):
            # Everything is served locally; nothing may be loaded from elsewhere.
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self'; "
                "font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
        return response
