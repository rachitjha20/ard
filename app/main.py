"""ARD web application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import documents, library, pages, system
from app.core.config.settings import APP_VERSION, FRONTEND_DIR, Settings, get_settings
from app.core.context import build_context
from app.core.logging import get_logger
from app.core.policy.engine import PolicyDenied
from app.core.security.session import LocalSecurityMiddleware, is_loopback_host, load_or_create_token
from app.services.documents.service import DocumentError
from app.services.documents.storage import StorageError, UploadTooLarge
from app.services.system import SystemService

log = get_logger("app")


class PdfStaticFiles(StaticFiles):
    """Static files with the MIME types PDF.js needs, whatever the OS mime table says."""

    _TYPES = {".mjs": "text/javascript", ".wasm": "application/wasm", ".bcmap": "application/octet-stream",
              ".ftl": "text/plain", ".pfb": "application/octet-stream", ".icc": "application/vnd.iccprofile"}

    def file_response(self, full_path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        response = super().file_response(full_path, *args, **kwargs)
        for suffix, media_type in self._TYPES.items():
            if str(full_path).endswith(suffix):
                response.headers["content-type"] = media_type
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.allow_non_loopback and not is_loopback_host(settings.host):
        raise RuntimeError(
            f"Refusing to bind to {settings.host}. ARD listens on this computer only. "
            "Set ARD_ALLOW_NON_LOOPBACK=true only if you understand the exposure."
        )

    context = build_context(settings)
    token = load_or_create_token(settings.token_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # noqa: ANN202
        context.start()
        log.info("ARD started", extra={"data": {"version": APP_VERSION, "bind": f"{settings.host}:{settings.port}",
                                                "safe_mode": context.policy.safe_mode}})
        try:
            yield
        finally:
            context.stop()
            log.info("ARD stopped")

    app = FastAPI(title="ARD", version=APP_VERSION, lifespan=lifespan,
                  description="Local-first document intelligence and PDF productivity platform.")
    app.state.context = context
    app.state.system = SystemService(context)

    app.add_middleware(LocalSecurityMiddleware, token=token)
    app.mount("/static", PdfStaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")
    app.include_router(pages.router)
    app.include_router(system.router)
    app.include_router(library.router)
    app.include_router(documents.router)

    @app.exception_handler(DocumentError)
    async def _document_error(_request: Request, exc: DocumentError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(UploadTooLarge)
    async def _too_large(_request: Request, exc: UploadTooLarge) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=413)

    @app.exception_handler(StorageError)
    async def _storage_error(_request: Request, exc: StorageError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PolicyDenied)
    async def _denied(_request: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.decision.reason, "action": exc.action,
             "requires_approval": exc.decision.requires_approval, "safe_mode": context.policy.safe_mode},
            status_code=403,
        )

    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn app.main:app_factory --factory``."""
    return create_app()
