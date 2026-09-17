"""Database setup. SQLite is the local default; PostgreSQL is supported by URL."""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.getenv("AGENTFORGE_DATABASE_URL", "sqlite:///./agentforge.db")


def _build_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure_database(url: str) -> None:
    """Replace the engine for tests or an explicitly configured deployment."""
    global engine, SessionLocal, DATABASE_URL
    DATABASE_URL = url
    engine = _build_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create the local development schema, never an implicit production schema."""
    environment = os.getenv("AGENTFORGE_ENV", "development").lower()
    if environment == "production":
        raise RuntimeError("production startup refuses auto-created schema; run 'alembic upgrade head'")
    # create_all cannot migrate an existing deployment. Refuse partial/stale
    # schemas rather than silently starting with missing columns.
    if set(inspect(engine).get_table_names()) & set(Base.metadata.tables):
        verify_schema()
    else:
        Base.metadata.create_all(bind=engine)
        verify_schema()


SCHEMA_REVISION = "c4d5e6f7a8b9"


def verify_schema(*, require_migrations: bool = False) -> None:
    """Check required tables/columns; production also requires the migration head.

    Development create_all databases may be unversioned, but they must still
    have every column used by the current models. This is a readiness check,
    not a replacement for migrations or a complete type/index drift audit.
    """
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = sorted(set(Base.metadata.tables) - present)
    for name, table in Base.metadata.tables.items():
        if name in present:
            columns = {column["name"] for column in inspector.get_columns(name)}
            missing.extend(f"{name}.{column.name}" for column in table.columns if column.name not in columns)
    if missing:
        raise RuntimeError(
            "database schema is not ready; run 'alembic upgrade head' "
            f"(missing: {', '.join(missing)})"
        )
    if require_migrations:
        with engine.connect() as connection:
            revisions = (set(connection.scalars(text("SELECT version_num FROM alembic_version")))
                         if "alembic_version" in present else set())
        if revisions != {SCHEMA_REVISION}:
            raise RuntimeError("database migration revision is not ready; run 'alembic upgrade head'")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
