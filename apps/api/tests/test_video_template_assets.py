"""The compiled-template assets are actually served (§19.2).

Worth its own file because the first implementation *built* a URL that looked right and
404'd: the route is project-scoped but the URL omitted the project id. Nothing in the
response shape looked wrong, so the only test that catches it is one that fetches the
URL the response hands back instead of constructing one.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_corrections import seed_version

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_PSD = REPO_ROOT / "samples" / "播客模板.psd"


@pytest.fixture(scope="module")
def db_app(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    """One client for the whole module: the real compile takes ~6 s."""
    from animatter.config import Settings
    from animatter.main import create_app

    data_dir = tmp_path_factory.mktemp("assets") / "data"
    app = create_app(Settings(data_dir=data_dir, host="127.0.0.1", log_level="ERROR"))
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def imported(db_app: TestClient) -> tuple[str, str, dict[str, object]]:
    """Import the real PSD once and wait for the queued compile to finish.

    The compile is driven by the router's background pool, so this polls the same
    endpoint the UI polls instead of calling ``compile_now`` itself. Doing both at once
    is what produced ``database is locked``: the worker holds a SQLite write
    transaction for the ~6 s it composites, and a second writer cannot have the lock.
    One import for the whole module for the same reason — seven concurrent compiles of
    a 58-layer PSD is not a test, it is a load generator.
    """
    if not SAMPLE_PSD.is_file():
        pytest.skip("samples/播客模板.psd 不在仓库里")

    db = db_app.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = seed_version(session, text="剧集标签很好用")
        project_id = version.project_id
        version_id = version.id
        session.commit()

    response = db_app.post(
        f"/api/projects/{project_id}/video/template", json={"path": str(SAMPLE_PSD)}
    )
    assert response.status_code == 201, response.text

    deadline = time.monotonic() + 120
    body: dict[str, object] = response.json()
    while time.monotonic() < deadline:
        body = db_app.get(f"/api/projects/{project_id}/video/template").json()
        if body["status"] not in {"pending", "compiling"}:
            break
        time.sleep(0.5)
    return project_id, version_id, body


def test_compiled_template_reports_ready(imported: tuple[str, str, dict[str, object]]) -> None:
    _project_id, _version_id, body = imported
    assert body["status"] == "ready", body
    assert body["box_rect"] == {"x": 240, "y": 209, "w": 1440, "h": 810}


def test_background_url_is_fetchable(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    """The URL in the response must work as-is — that is the bug this pins."""
    _project_id, _version_id, body = imported
    url = str(body["background_url"])
    assert url, "a ready template must expose its background"

    response = db_app.get(url)
    assert response.status_code == 200, f"{url} -> {response.status_code}"
    assert response.headers["content-type"].startswith("image/png")
    assert len(response.content) > 10_000


def test_overlay_url_is_fetchable_and_tiny(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    """The overlay is just the frame stroke, so it must be small — not a whole poster."""
    _project_id, _version_id, body = imported
    response = db_app.get(str(body["overlay_url"]))
    assert response.status_code == 200
    # A near-empty RGBA PNG at 1920×1080 compresses far below the background's 300 KB.
    assert len(response.content) < 60_000


def test_avatar_slots_carry_both_states(imported: tuple[str, str, dict[str, object]]) -> None:
    _project_id, _version_id, body = imported
    slots = body["slots"]
    assert isinstance(slots, dict)
    avatars = slots["avatars"]
    assert isinstance(avatars, list) and len(avatars) == 4
    for slot in avatars:
        assert Path(str(slot["lit_path"])).is_file()
        assert Path(str(slot["dim_path"])).is_file()


def test_asset_route_rejects_a_traversal_attempt(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, _body = imported
    response = db_app.get(f"/api/projects/{project_id}/video/template/asset/..%2F..%2Fapp.db")
    assert response.status_code == 404
    assert not response.content.startswith(b"SQLite")


def test_asset_route_of_another_project_is_not_served(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    """The route is project-scoped: one project must not serve another's assets."""
    _project_id, _version_id, _body = imported
    db = db_app.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        other = seed_version(session, text="别的项目")
        other_id = other.project_id
        session.commit()

    response = db_app.get(f"/api/projects/{other_id}/video/template/asset/background.png")
    assert response.status_code == 409  # this project has no compiled template


def test_reimporting_the_same_psd_reuses_the_compiled_artefacts(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    """The compile depends only on the bytes, so a matching hash must skip it."""
    project_id, _version_id, before = imported
    response = db_app.post(
        f"/api/projects/{project_id}/video/template", json={"path": str(SAMPLE_PSD)}
    )
    assert response.status_code == 201
    after = response.json()
    assert after["id"] == before["id"]
    assert after["status"] == "ready"
    assert after["compiled_at"] == before["compiled_at"]
