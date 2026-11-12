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
    # ``dashscope_api_key=None`` explicitly, even though ``.env`` on a developer's machine has
    # a real key: the class reads ``.env`` at import time, so without this every test that
    # reaches a model-backed endpoint would make a **real paid call**. Round 100 made deck
    # generation one of those endpoints, which is what turned a silently expensive test suite
    # into a visible failure (one test got a one-page outline from a three-sentence fixture).
    # A test that wants a key sets one itself and stubs the transport.
    return Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        log_level="ERROR",
        dashscope_api_key=None,
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
