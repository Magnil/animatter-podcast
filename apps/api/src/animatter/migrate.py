"""Alembic migration runner.

Migrations are applied at startup: the schema is part of the deliverable, and a
half-migrated local database produces confusing 500s. ``alembic upgrade head``
is idempotent, so this costs a few milliseconds per boot.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from animatter.config import Settings
from animatter.log import get_logger

log = get_logger(__name__)

API_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(settings: Settings) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def upgrade_to_head(settings: Settings) -> None:
    settings.ensure_dirs()
    log.info("migrations.start", db=str(settings.db_path))
    command.upgrade(alembic_config(settings), "head")
    log.info("migrations.done")
