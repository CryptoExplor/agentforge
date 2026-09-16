"""Alembic environment for AgentForge's SQLAlchemy metadata."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from logging.config import fileConfig

server_dir = str(Path(__file__).resolve().parents[1] / "server")
if server_dir not in sys.path:
    sys.path.insert(0, server_dir)

from alembic import context
from sqlalchemy import engine_from_config, pool

from agentforge_server.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # Keep credentials out of alembic.ini; deployment supplies the URL through
    # the same variable used by the application.
    return os.getenv("AGENTFORGE_DATABASE_URL", "sqlite:///./agentforge.db")


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
