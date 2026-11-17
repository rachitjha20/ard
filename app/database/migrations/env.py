"""Alembic environment for ARD (SQLite, batch mode for ALTER support)."""

from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.database.session import Base

target_metadata = Base.metadata


def _include(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    # The full-text index and its shadow tables are created by hand in a migration, not from models.
    return not (type_ == "table" and name.startswith("page_fts"))


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                          include_object=_include)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
