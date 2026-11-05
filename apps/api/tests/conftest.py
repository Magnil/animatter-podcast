"""Shared pytest fixtures: an isolated data directory per test module."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.main import create_app


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        log_level="ERROR",
    )


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def session(client: TestClient) -> Iterator[Session]:
    """A Session bound to the test client's database.

    Shared rather than per-module: several suites need to call service functions
    directly against the same data the HTTP client sees.
    """
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s
