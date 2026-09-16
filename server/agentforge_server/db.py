"""Database setup. SQLite is the local default; PostgreSQL is supported by URL."""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect
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
    Base.metadata.create_all(bind=engine)


def verify_schema() -> None:
    """Fail closed when a migration has not provisioned the configured database."""
    required = set(Base.metadata.tables)
    present = set(inspect(engine).get_table_names())
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(
            "database schema is not ready; run 'alembic upgrade head' "
            f"(missing: {', '.join(missing)})"
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
