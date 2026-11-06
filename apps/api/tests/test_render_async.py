"""Rendering as a background job instead of a blocking request.

The regression these guard against, in the user's words: 「导出成片音频 点击后
无反应」. The button was in fact working — the encode was simply running inside the
request for twelve minutes with nothing observable happening, and while it ran it
held a SQLite write transaction that made every *other* write fail with
``database is locked``.

So the properties worth pinning are not "does ffmpeg produce a file" (that is
``test_render.py``) but:

* the POST returns immediately, with a job id and a status to poll;
* progress is observable while the encode runs;
* an unrelated write succeeds *during* the encode;
* a second click attaches to the running job instead of starting a competitor;
* a job left ``running`` by a dead process is swept on the next boot.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_render import make_audio

from animatter.config import Settings
from animatter.ids import new_id, utcnow_iso
from animatter.models import Media, Render
from animatter.services import clipping as clipping_service
from animatter.services import renders as renders_service


@pytest.fixture()
def rendered_project(client: TestClient, session: Session, settings: Settings):  # type: ignore[no-untyped-def]
    """A seeded version whose media points at a real, short WAV."""
    from tests.test_corrections import seed_version

    version = seed_version(session, text="一二三四五六七八", sizes=[8])
    media = session.get(Media, version.media_id)
    assert media is not None
    source = make_audio(settings.data_dir / "src.wav", seconds=2.0)
    media.path = str(source)
    media.duration_ms = 2000
    media.sample_rate = 22050
    media.channels = 1
    session.flush()
    clipping_service.add_deletion(session, version.id, start_ms=200, end_ms=400)
    session.commit()
    return version


def _wait(client: TestClient, render_id: str, *, timeout: float = 120.0) -> dict[str, object]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/renders/{render_id}").json()
        if payload["status"] in {"done", "failed"}:
            return payload
        time.sleep(0.2)
    raise AssertionError(f"渲染没有在 {timeout}s 内结束")


def test_post_returns_a_job_without_waiting(
    client: TestClient, rendered_project, settings: Settings  # type: ignore[no-untyped-def]
) -> None:
    started = time.time()
    response = client.post(f"/api/versions/{rendered_project.id}/render")
    elapsed = time.time() - started

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"queued", "running", "done"}
    assert payload["url"].endswith(f"/api/renders/{payload['id']}/file")
    # Two seconds of audio encodes in well under a second, so the timing bound
    # only proves "not the old synchronous path"; the real 12-minute case is
    # covered by the progress assertions below.
    assert elapsed < 5.0


def test_progress_is_observable_and_ends_at_one(
    client: TestClient, rendered_project  # type: ignore[no-untyped-def]
) -> None:
    payload = client.post(f"/api/versions/{rendered_project.id}/render").json()
    final = _wait(client, payload["id"])

    assert final["status"] == "done", final
    assert final["progress"] == 1.0
    assert final["duration_ms"] > 0
    assert final["error"] is None


def test_download_serves_the_rendered_file(
    client: TestClient, rendered_project  # type: ignore[no-untyped-def]
) -> None:
    payload = client.post(f"/api/versions/{rendered_project.id}/render").json()
    final = _wait(client, payload["id"])
    assert final["status"] == "done"

    response = client.get(final["url"])
    assert response.status_code == 200
    assert len(response.content) > 1000


def test_other_writes_succeed_while_rendering(
    client: TestClient, session: Session, rendered_project  # type: ignore[no-untyped-def]
) -> None:
    """The SQLite write lock must be free while ffmpeg works.

    This is the failure that actually happened: a second render request died with
    ``database is locked`` because the first held an open write transaction for
    the whole encode.
    """
    payload = client.post(f"/api/versions/{rendered_project.id}/render").json()

    writes = []
    for _ in range(5):
        response = client.patch("/api/settings", json={"values": {"snap_to_silence": True}})
        writes.append(response.status_code)
        if client.get(f"/api/renders/{payload['id']}").json()["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)

    assert writes == [200] * len(writes), writes
    _wait(client, payload["id"])


def test_second_click_attaches_to_the_running_job(
    client: TestClient, rendered_project  # type: ignore[no-untyped-def]
) -> None:
    first = client.post(f"/api/versions/{rendered_project.id}/render").json()
    second = client.post(f"/api/versions/{rendered_project.id}/render").json()

    # Either it attached to the same job, or the first had already completed and
    # the second reused the cached file — never two encoders on one target.
    if first["status"] in {"queued", "running"} and second["status"] in {"queued", "running"}:
        assert second["id"] == first["id"]
    _wait(client, second["id"])


def test_orphaned_jobs_are_failed_on_boot(client: TestClient, session: Session) -> None:
    session.add(
        Render(
            id=new_id("rnd"),
            project_id="prj_x",
            version_id="ver_x",
            edl_hash="deadbeef",
            out_path="",
            duration_ms=0,
            status="running",
            progress=0.4,
            created_at=utcnow_iso(),
        )
    )
    session.commit()

    db = client.app.state.db  # type: ignore[attr-defined]
    assert renders_service.fail_orphaned(db) == 1

    row = session.get(Render, session.query(Render).order_by(Render.created_at.desc()).first().id)
    assert row is not None
    assert row.status == "failed"
    assert "中断" in (row.error or "")


def test_render_status_lists_a_known_job(client: TestClient, rendered_project) -> None:  # type: ignore[no-untyped-def]
    payload = client.post(f"/api/versions/{rendered_project.id}/render").json()
    _wait(client, payload["id"])
    again = client.get(f"/api/renders/{payload['id']}")
    assert again.status_code == 200
    assert again.json()["id"] == payload["id"]


def test_unknown_render_is_404_not_a_crash(client: TestClient) -> None:
    response = client.get("/api/renders/rnd_does_not_exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "render_not_found"
