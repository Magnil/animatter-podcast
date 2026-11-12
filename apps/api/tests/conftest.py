"""Shared pytest fixtures: an isolated data directory per test.

**The database arrives already migrated.** Every test that takes ``client``/``session`` used
to replay the whole Alembic chain (18 revisions) on a database that had just been created by
``settings.ensure_dirs()`` — measured at ~440 ms of a ~467 ms fixture, 253 s of a 371 s suite,
i.e. two thirds of the wall clock spent rebuilding the same schema 439 times.

So the chain is run **once** per session into a template database, and each test gets a
copy of it (1.1 ms). The app's own ``upgrade_to_head`` in the lifespan then finds nothing to
do (20 ms), which is also what a returning user's machine looks like — the fixture is now
closer to production, not further from it.

What this deliberately does *not* skip: ``test_migrations.py`` still drives ``alembic`` from
an empty database through named revisions, because "the migration chain works" is a property
of the code, not of the fixture.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.main import create_app
from animatter.migrate import upgrade_to_head


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database at ``head``, built once and copied into every test's data dir.

    Session-scoped on purpose: the point is to pay for the 18 migrations once. The file is
    copied (not linked), so a test that corrupts its database cannot affect the next one.
    """
    root = tmp_path_factory.mktemp("migrated")
    seed = Settings(
        data_dir=root / "seed",
        host="127.0.0.1",
        log_level="ERROR",
        dashscope_api_key=None,
    )
    upgrade_to_head(seed)
    return seed.db_path


@pytest.fixture()
def settings(tmp_path: Path, migrated_template: Path) -> Settings:
    # ``dashscope_api_key=None`` explicitly, even though ``.env`` on a developer's machine has
    # a real key: the class reads ``.env`` at import time, so without this every test that
    # reaches a model-backed endpoint would make a **real paid call**. Round 100 made deck
    # generation one of those endpoints, which is what turned a silently expensive test suite
    # into a visible failure (one test got a one-page outline from a three-sentence fixture).
    # A test that wants a key sets one itself and stubs the transport.
    #
    # ``fixture-data``, not ``data``: a few test files build their *own* ``Settings(data_dir=
    # tmp_path / "data")`` to run a second app (``keyed_client`` in test_outline_import is one).
    # Sharing the name put two engines on one SQLite file, and copying the template over a
    # database the other app already had open produced ``database is locked`` at fixture setup.
    resolved = Settings(
        data_dir=tmp_path / "fixture-data",
        host="127.0.0.1",
        log_level="ERROR",
        dashscope_api_key=None,
    )
    resolved.ensure_dirs()
    shutil.copyfile(migrated_template, resolved.db_path)
    return resolved


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
