"""ARD command line.

The CLI builds the same application context as the web app and calls the same
services, so the policy engine and the audit log apply here too.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer

from app.core.config.settings import APP_VERSION, get_settings
from app.core.policy.engine import Actor, PolicyDenied
from app.services.documents.service import DocumentError
from app.services.documents.storage import StorageError

cli = typer.Typer(help="ARD: local-first PDF and document workspace.", no_args_is_help=True)
ACTOR = Actor(name="local-user", interface="cli")


@contextmanager
def _context() -> Iterator:  # noqa: ANN001
    from app.core.context import build_context
    from app.services.system import SystemService

    context = build_context()
    context.start()
    try:
        yield context, SystemService(context)
    except (DocumentError, StorageError, PolicyDenied) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        context.stop()


@cli.command()
def serve(port: int = typer.Option(None, help="Port to listen on."),
          open_browser: bool = typer.Option(False, "--open", help="Open ARD in the default browser.")) -> None:
    """Start ARD on this computer (loopback only)."""
    import uvicorn

    from app.main import create_app

    settings = get_settings()
    if port:
        settings.port = port
    url = f"http://{settings.host}:{settings.port}"
    typer.echo(f"ARD {APP_VERSION} is starting at {url}")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


@cli.command("import")
def import_files(files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True)) -> None:
    """Import PDF files into the library. The files you pass are copied, never changed."""
    with _context() as (context, _system):
        for path in files:
            try:
                with path.open("rb") as handle:
                    result = context.documents.import_document(ACTOR, handle, path.name)
            except (DocumentError, StorageError) as exc:
                typer.secho(f"skipped  {path.name}: {exc}", fg=typer.colors.YELLOW)
                continue
            document = result["document"]
            note = "already in library" if result["already_imported"] else "imported"
            typer.echo(f"{note}  {document['id']}  {document['display_name']}  ({document['page_count']} pages)")


@cli.command("list")
def list_documents(query: str = typer.Option("", "--query", "-q"),
                   as_json: bool = typer.Option(False, "--json")) -> None:
    """List documents in the library."""
    with _context() as (context, _system):
        result = context.documents.list_documents(ACTOR, query=query)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    for document in result["documents"]:
        typer.echo(f"{document['id']}  {document['page_count'] or '?':>5} pages  v{document['current_version_number']}"
                   f"  {document['display_name']}")
    typer.echo(f"{result['total']} document(s)")


@cli.command()
def capabilities() -> None:
    """Show which engines are available and how to enable the rest."""
    with _context() as (_context_, system):
        for item in system.capabilities(ACTOR, refresh=True)["integrations"]:
            mark = "yes" if item["available"] else "no "
            line = f"[{mark}] {item['name']:<22} {item['version'] or ''}"
            if not item["available"] and item["enable_hint"]:
                line += f"  ->  {item['enable_hint']}"
            typer.echo(line)


@cli.command("verify-audit")
def verify_audit() -> None:
    """Check the hash-linked audit log."""
    with _context() as (_context_, system):
        result = system.verify_audit(ACTOR)
    typer.echo(f"{result['status']}: {result['records']} record(s)")
    if not result["ok"]:
        typer.secho(f"First problem at record {result['first_bad_seq']}: {result['problem']}", fg=typer.colors.RED)
        raise typer.Exit(2)


@cli.command()
def diagnostics() -> None:
    """Print diagnostic information (no document content, no secrets)."""
    with _context() as (_context_, system):
        typer.echo(json.dumps(system.diagnostics(ACTOR), indent=2, default=str))


if __name__ == "__main__":
    cli()
