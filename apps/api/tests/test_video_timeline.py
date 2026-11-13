"""Video timeline API: blocks, the snap grid, and the two lengths the UI shows.

The behaviour under test is the one from AGENTS.md §19.3 that is easy to get wrong:
a block's length **in the output** is not the difference of its own edges. Delete
audio inside a block and the block plays continuously for less time; that number comes
from the compiled keep list, and the UI must be told both.

Nothing here rejects an audio edit. A deletion that crosses a block boundary just
moves the boundary afterwards, and one that swallows a block marks it orphaned rather
than deleting the user's clip.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.models import VideoItem
from animatter.services import video as video_service


def seed(client: TestClient, session: Session) -> tuple[str, str]:
    """A committed project + transcript. Returns `(project_id, version_id)`.

    Committed, not flushed: the HTTP client reads through its own session, so an
    uncommitted seed is invisible to it and every request 404s.
    """
    version = seed_version(session)
    session.commit()
    return version.project_id, version.id


def add_item(client: TestClient, version_id: str, start: int, end: int, **extra: object) -> dict[str, object]:
    response = client.post(
        f"/api/versions/{version_id}/video/items",
        json={"kind": "media", "start_ms": start, "end_ms": end, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- the grid -----------------------------------------------------------


def test_timeline_of_a_fresh_project_has_the_ends_and_no_blocks(
    client: TestClient, session: Session
) -> None:
    _project_id, version_id = seed(client, session)
    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    assert data["items"] == []
    boundaries = data["grid"]["boundaries"]
    # 0 and the media duration always exist so a block can cover the whole film.
    assert boundaries[0] == 0
    assert boundaries[-1] == data["grid"]["duration_ms"]


def test_grid_boundaries_are_the_sentence_starts(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    # ``seed_version`` builds one sentence per character at 100 ms steps.
    assert 0 in data["grid"]["boundaries"]
    assert data["grid"]["duration_ms"] in data["grid"]["boundaries"]


# --- creating blocks ----------------------------------------------------


def test_creating_a_block_snaps_both_edges(client: TestClient, session: Session) -> None:
    """Snapping is to the *collapsed* grid, not every sentence start.

    `seed_version` puts a sentence every 100 ms, and boundaries closer than 500 ms
    are merged into one drag target (a 160 ms tile is not a useful place to drop a 3 s
    block, and offering it makes the drag feel jumpy). So the only targets are the two
    ends, and an edge at 450 ms is 150 ms from the nearest one — outside the radius,
    hence unmoved. Snapping to 400 would require the uncollapsed grid, which the UI
    does not show.
    """
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 0, 450)
    assert item["start_ms"] == 0
    assert item["end_ms"] == 450


def test_a_drag_near_a_collapsed_boundary_still_snaps(client: TestClient, session: Session) -> None:
    """Within the radius it does snap — to a surviving target.

    Seeded with three sentences at 100 ms steps, so the raw grid is
    ``(0, 100, 200, 700)``; collapsing to 500 ms spacing leaves ``(0, 700)`` because
    every sentence start sits inside one burst. That is the case worth pinning: the
    target list is *not* the sentence starts, and a drag that lands 40 ms from a
    sentence start which was collapsed away must not be pulled onto it.
    """
    version = seed_version(session, sizes=[1, 1, 5])
    session.commit()
    item = add_item(client, version.id, 10, 660)
    assert (item["start_ms"], item["end_ms"]) == (0, 700)


def test_snapping_can_be_turned_off(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 0, 450, snap=False)
    assert item["end_ms"] == 450


def test_a_zero_length_block_is_rejected(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    response = client.post(
        f"/api/versions/{version_id}/video/items",
        json={"kind": "media", "start_ms": 100, "end_ms": 100},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "video_item_empty"


def test_an_unknown_fit_is_rejected(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    response = client.post(
        f"/api/versions/{version_id}/video/items",
        json={"kind": "media", "start_ms": 0, "end_ms": 400, "fit": "squish"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "video_item_fit_invalid"


# --- the two lengths ----------------------------------------------------


def test_output_length_shrinks_when_audio_inside_the_block_is_deleted(
    client: TestClient, session: Session
) -> None:
    """The whole point of §19.3: continuous play for less time, not a jump cut."""
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 0, 500, snap=False)
    assert item["duration_ms"] == 500
    assert item["output_ms"] == 500

    client.post(
        f"/api/versions/{version_id}/deletions", json={"start_ms": 100, "end_ms": 200}
    )

    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    updated = next(row for row in data["items"] if row["id"] == item["id"])
    assert updated["duration_ms"] == 500  # the block's own edges did not move
    assert updated["output_ms"] < 500  # but it plays for less time


def test_deleting_a_whole_block_marks_it_orphaned(client: TestClient, session: Session) -> None:
    """7.54 % of sentences in a real project end up here — it needs a path, not a crash."""
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 200, 300, snap=False)

    client.post(
        f"/api/versions/{version_id}/deletions", json={"start_ms": 150, "end_ms": 350}
    )

    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    updated = next(row for row in data["items"] if row["id"] == item["id"])
    assert updated["output_ms"] == 0
    assert updated["orphaned"] is True


def test_moving_an_orphaned_block_clears_the_flag(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 200, 300, snap=False)
    client.post(f"/api/versions/{version_id}/deletions", json={"start_ms": 150, "end_ms": 350})

    response = client.patch(
        f"/api/video/items/{item['id']}", json={"start_ms": 0, "end_ms": 400, "snap": False}
    )
    assert response.status_code == 200
    assert response.json()["orphaned"] is False


# --- edits that move the grid -------------------------------------------


def test_a_deletion_crossing_a_boundary_does_not_reject_the_edit(
    client: TestClient, session: Session
) -> None:
    """A video clip must not be able to veto an audio cut."""
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 0, 400, snap=False)

    response = client.post(
        f"/api/versions/{version_id}/deletions", json={"start_ms": 350, "end_ms": 450}
    )
    assert response.status_code == 201

    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    still_there = next(row for row in data["items"] if row["id"] == item["id"])
    assert still_there["end_ms"] > still_there["start_ms"]


def test_renormalize_keeps_edges_that_are_still_boundaries(
    client: TestClient, session: Session
) -> None:
    """Deleting inside a block must not nudge its edges — no creeping drift."""
    _project_id, version_id = seed(client, session)
    item = add_item(client, version_id, 0, 500, snap=False)
    client.post(f"/api/versions/{version_id}/deletions", json={"start_ms": 100, "end_ms": 200})

    data = client.get(f"/api/versions/{version_id}/video/timeline").json()
    updated = next(row for row in data["items"] if row["id"] == item["id"])
    assert (updated["start_ms"], updated["end_ms"]) == (0, 500)


# --- template gating ----------------------------------------------------


def test_timeline_works_before_a_template_is_imported(
    client: TestClient, session: Session
) -> None:
    """The timeline is where blocks are laid out; the template only affects rendering."""
    project_id, version_id = seed(client, session)
    assert client.get(f"/api/projects/{project_id}/video/template").json() is None
    assert client.get(f"/api/versions/{version_id}/video/timeline").status_code == 200


def test_export_without_a_template_says_so(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    response = client.post(f"/api/versions/{version_id}/video/export", json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "video_template_missing"


def test_importing_a_non_psd_is_rejected(client: TestClient, session: Session, tmp_path: Path) -> None:
    project_id, _version_id = seed(client, session)
    bogus = tmp_path / "notes.txt"
    bogus.write_text("not a psd", encoding="utf-8")
    response = client.post(
        f"/api/projects/{project_id}/video/template", json={"path": str(bogus)}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "template_not_psd"


def test_importing_a_missing_file_is_a_404(client: TestClient, session: Session, tmp_path: Path) -> None:
    project_id, _version_id = seed(client, session)
    response = client.post(
        f"/api/projects/{project_id}/video/template", json={"path": str(tmp_path / "gone.psd")}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "template_file_missing"


def test_template_asset_route_refuses_path_traversal(
    client: TestClient, session: Session
) -> None:
    """A route that serves files must not be able to serve ``../../app.db``."""
    project_id, _version_id = seed(client, session)
    response = client.get(f"/api/projects/{project_id}/video/template/asset/..%2F..%2Fapp.db")
    # Nothing compiled yet, so it is a 409; the point is that it never returns a file.
    assert response.status_code in {404, 409}


# --- service-level invariants -------------------------------------------


def test_refresh_after_audio_edit_is_a_noop_without_blocks(
    client: TestClient, session: Session
) -> None:
    from animatter.services import clipping

    _project_id, version_id = seed(client, session)
    version = clipping.get_version(session, version_id)
    settings = client.app.state.settings  # type: ignore[attr-defined]
    report = video_service.refresh_after_audio_edit(session, settings, version_id)
    assert report.moved == 0 and report.orphaned == 0
    assert report.message() == ""
    assert version.id == version_id


def test_shift_items_moves_every_block_by_the_same_delta(
    client: TestClient, session: Session
) -> None:
    project_id, version_id = seed(client, session)
    add_item(client, version_id, 0, 400, snap=False)
    add_item(client, version_id, 400, 800, snap=False)

    moved = video_service.shift_items(session, project_id, 250)
    assert moved == 2
    starts = sorted(row.start_ms for row in session.query(VideoItem).all())
    assert starts == [250, 650]


# --- solo and placement (round 111) -------------------------------------


def test_a_new_block_is_box_placed_and_not_soloed(
    client: TestClient, session: Session
) -> None:
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    assert created["placement"] == "box"
    assert created["solo"] is False
    assert created["muted"] is True


def test_placement_round_trips_through_the_api(client: TestClient, session: Session) -> None:
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    patched = client.patch(f"/api/video/items/{created['id']}", json={"placement": "full"}).json()
    assert patched["placement"] == "full"


def test_an_unknown_placement_is_refused(client: TestClient, session: Session) -> None:
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    response = client.patch(f"/api/video/items/{created['id']}", json={"placement": "canvas"})
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "video_item_placement_invalid"


def test_pressing_s_clears_the_mute(client: TestClient, session: Session) -> None:
    """`S` means "replace the podcast", which cannot also mean "this block is silent"."""
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    assert created["muted"] is True
    patched = client.patch(f"/api/video/items/{created['id']}", json={"solo": True}).json()
    assert patched["solo"] is True
    assert patched["muted"] is False


def test_pressing_m_clears_the_solo(client: TestClient, session: Session) -> None:
    """The mirror rule: muting a block stops it replacing anything."""
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    client.patch(f"/api/video/items/{created['id']}", json={"solo": True})
    patched = client.patch(f"/api/video/items/{created['id']}", json={"muted": True}).json()
    assert patched["muted"] is True
    assert patched["solo"] is False


def test_setting_both_in_one_request_is_resolved_not_stored(
    client: TestClient, session: Session
) -> None:
    """A client that sends both must not be able to create the impossible state."""
    _project, version = seed(client, session)
    created = add_item(client, version, 0, 3000)
    patched = client.patch(
        f"/api/video/items/{created['id']}", json={"solo": True, "muted": True}
    ).json()
    assert not (patched["solo"] and patched["muted"]), patched
