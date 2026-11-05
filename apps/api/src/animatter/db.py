"""SQLite engine construction and session plumbing.

WAL mode is mandatory: the editor autosaves on every edit operation while the
renderer reads the same database, and the default rollback journal would block
those readers for the duration of every write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from animatter.config import Settings


def build_engine(settings: Settings) -> Engine:
    """Create an engine whose connections carry the pragmas this app needs."""
    engine = create_engine(settings.database_url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return engine


class Database:
    """Owns the engine and hands out short-lived sessions."""

    def __init__(self, settings: Settings) -> None:
        settings.ensure_dirs()
        self._engine = build_engine(settings)
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=self._engine, expire_on_commit=False, future=True
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self._engine.dispose()
