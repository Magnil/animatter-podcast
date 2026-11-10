"""Video anchors survive the two edits that can move the sentence grid (§19.3).

Deleting audio does **not** move the grid: no token changes, so no sentence start
changes, and the block's shortened length is derived on read. Splitting or merging
sentences does move it, and so does a whole-timeline shift — but in different ways:

* a boundary edit can *delete* a boundary, so an edge sitting on it must be re-seated;
* a shift is rigid, so every block moves by the same delta and keeps its offset.

Getting the second one wrong is subtle: re-seating after a shift would snap a block
that deliberately started 40 ms after a sentence back onto the start, silently
changing an edit the user made on purpose.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.models import VideoItem


def seed(client: TestClient, session: Session, *, text: str = "剧集标签很好用") -> tuple[str, str]:
    version = seed_version(session, text=text)
    session.commit()
    return version.project_id, version.id


def add_item(client: TestClient, version_id: str, start: int, end: int) -> dict[str, object]:
    response = client.post(
        f"/api/versions/{version_id}/video/items",
        json={"kind": "media", "start_ms": start, "end_ms": end, "snap": False},
    )
    assert response.status_code == 201, response.text
    return response.json()


def items(session: Session) -> list[VideoItem]:
    session.expire_all()
    return sorted(session.query(VideoItem).all(), key=lambda row: row.start_ms)


def with_lead(session: Session, version_id: str, lead_ms: int) -> None:
    """Give the fixture leading silence so a shift is possible in both directions.

    ``seed_version`` puts the first token at 0, and a timeline that already begins at 0
    *cannot* move earlier: the clamp fires and the test would be measuring the clamp.
    Real episodes open with music or a breath, so the lead-in is the honest setup.
    """
    from animatter.models import Token, TranscriptVersion

    for row in session.query(Token).filter_by(version_id=version_id).all():
        row.start_ms += lead_ms
        row.end_ms += lead_ms
    version = session.get(TranscriptVersion, version_id)
    assert version is not None
    version.duration_ms += lead_ms
    session.commit()


# --- deletions do not touch the grid ------------------------------------


def test_deleting_words_leaves_block_edges_alone(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session)
    add_item(client, version_id, 100, 500)

    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": [3]})

    rows = items(session)
    assert (rows[0].start_ms, rows[0].end_ms) == (100, 500)


# --- boundary edits do --------------------------------------------------


def test_splitting_a_sentence_keeps_every_block_edge(client: TestClient, session: Session) -> None:
    """A split *adds* a boundary; nothing an existing edge sits on disappears."""
    _project_id, version_id = seed(client, session)
    add_item(client, version_id, 0, 400)

    response = client.post(
        f"/api/versions/{version_id}/boundaries",
        json={"action": "new_sentence", "from_token": 3, "to_token": 5},
    )
    assert response.status_code == 200, response.text

    rows = items(session)
    assert (rows[0].start_ms, rows[0].end_ms) == (0, 400)


def test_merging_sentences_reseats_an_orphaned_edge(client: TestClient, session: Session) -> None:
    """A merge deletes a boundary, so an edge on it has to land somewhere real.

    Seeded with two sentences meeting at 200 ms and a block ending exactly there.
    Merging them removes that start, so the edge must move to a surviving boundary
    rather than keep pointing at a cut point that no longer exists.
    """
    version = seed_version(session, text="剧集标签", sizes=[2, 2])
    session.commit()
    version_id = version.id

    add_item(client, version_id, 0, 200)
    response = client.post(
        f"/api/versions/{version_id}/boundaries",
        json={"action": "merge", "from_token": 1, "to_token": 3},
    )
    assert response.status_code == 200, response.text

    rows = items(session)
    assert rows, "the block must not be deleted by an audio edit"
    boundaries = client.get(f"/api/versions/{version_id}/video/timeline").json()["grid"]["boundaries"]
    assert 200 not in boundaries  # the merge really did remove that boundary
    assert rows[0].end_ms in boundaries


# --- the rigid shift ----------------------------------------------------


def test_time_shift_moves_blocks_by_the_same_delta(client: TestClient, session: Session) -> None:
    _project_id, version_id = seed(client, session, text="嗯嗯剧集标签很好用")
    with_lead(session, version_id, 500)
    add_item(client, version_id, 600, 1000)

    # Negative = earlier, the usual case (the ASR ran late) and the direction the
    # leading silence leaves room for.
    response = client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -200})
    assert response.status_code == 200, response.text
    delta = response.json()["delta_ms"]
    assert delta == -200

    rows = items(session)
    assert (rows[0].start_ms, rows[0].end_ms) == (600 + delta, 1000 + delta)


def test_time_shift_preserves_an_edge_that_sat_between_boundaries(
    client: TestClient, session: Session
) -> None:
    """The shift is rigid — re-seating here would silently rewrite a human's offset."""
    _project_id, version_id = seed(client, session, text="嗯嗯剧集标签很好用")
    with_lead(session, version_id, 500)
    add_item(client, version_id, 630, 970)  # deliberately not on a boundary

    before = items(session)[0]
    offset = before.start_ms

    response = client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -100})
    delta = response.json()["delta_ms"]

    after = items(session)[0]
    assert after.start_ms - offset == delta
    assert (after.start_ms, after.end_ms) == (630 + delta, 970 + delta)


def test_a_shift_that_gets_clamped_moves_blocks_by_the_clamped_delta(
    client: TestClient, session: Session
) -> None:
    """The reported ``delta_ms`` is the one actually applied, and it is what blocks use."""
    _project_id, version_id = seed(client, session, text="剧集标签很好用")
    with_lead(session, version_id, 500)
    add_item(client, version_id, 600, 1000)

    response = client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -1000})
    body = response.json()
    applied = body["delta_ms"]

    rows = items(session)
    assert (rows[0].start_ms, rows[0].end_ms) == (600 + applied, 1000 + applied)
