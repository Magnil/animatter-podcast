"""Timeline alignment: apply, undo, clamp, and never break a cut.

The bug this feature exists for: Bailian's word timestamps run late (measured
median −65 ms on 997 real sentence heads), so 「播放这句」 swallowed the first
syllable. The fix is a constant shift of the whole timeline.

The tests below are mostly about the *contract* rather than the arithmetic,
because the arithmetic is one addition. What can go wrong is:

* applying the same request twice and getting 160 ms instead of 80;
* shifting tokens but not deletions, so every cut slides off the word it was
  made for;
* clamping rows individually, which silently compresses the head of the file;
* losing a human's own edge drag because the shift was recomputed from scratch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.models import Deletion, Token, TranscriptVersion
from animatter.services import align as align_service


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def token_times(session: Session, version_id: str) -> list[tuple[int, int]]:
    rows = session.query(Token).filter_by(version_id=version_id).order_by(Token.idx).all()
    return [(row.start_ms, row.end_ms) for row in rows]


def with_lead(session: Session, version_id: str, lead_ms: int) -> None:
    """Give the fixture leading silence, the way a real recording has.

    ``seed_version`` puts the first token at 0, and a timeline that already
    begins at 0 *cannot* move earlier: the clamp fires and the test would be
    measuring the clamp instead of the shift. Real episodes open with music or a
    breath, so the lead-in is the honest setup rather than a workaround.

    ``duration_ms`` grows with it — otherwise undoing the shift would be clamped
    by the media length instead, which is a different clamp than the one these
    tests are about.
    """
    for row in session.query(Token).filter_by(version_id=version_id).all():
        row.start_ms += lead_ms
        row.end_ms += lead_ms
    version = session.get(TranscriptVersion, version_id)
    assert version is not None
    version.duration_ms += lead_ms
    session.flush()


def add_deletion(session: Session, version_id: str, start: int, end: int) -> None:
    from animatter.ids import new_id, utcnow_iso

    session.add(
        Deletion(
            id=new_id("del"),
            version_id=version_id,
            start_ms=start,
            end_ms=end,
            source="token",
            token_ids_json="[]",
            created_at=utcnow_iso(),
            removed=False,
        )
    )
    session.flush()


def make_version(session: Session, *, text: str, sizes: list[int], step: int = 100) -> TranscriptVersion:
    """A version with a 400 ms lead-in, so every shift test can actually move."""
    version = seed_version(session, text=text, sizes=sizes, step=step)
    with_lead(session, version.id, 400)
    return version


# --- applying -----------------------------------------------------------


def test_shift_moves_every_token(session: Session) -> None:
    version = make_version(session, text="一二三四", sizes=[2, 2])
    before = token_times(session, version.id)

    align_service.apply_shift(session, version.id, shift_ms=-80)

    after = token_times(session, version.id)
    assert all(b[0] - 80 == a[0] and b[1] - 80 == a[1] for b, a in zip(before, after, strict=True))


def test_shift_is_absolute_not_relative(session: Session) -> None:
    """Applying −80 twice must leave the timeline at −80, not −160.

    This is the whole reason the API takes a target instead of a step: the panel
    offers 「对齐到 −80」 and 「撤销」, and a user clicking twice must not end up
    with twice the offset they asked for.
    """
    version = make_version(session, text="一二三四", sizes=[4])

    align_service.apply_shift(session, version.id, shift_ms=-80)
    once = token_times(session, version.id)
    align_service.apply_shift(session, version.id, shift_ms=-80)
    twice = token_times(session, version.id)

    assert once == twice
    reloaded = session.get(TranscriptVersion, version.id)
    assert reloaded is not None
    assert reloaded.time_shift_ms == -80


def test_undo_restores_original_times(session: Session) -> None:
    version = make_version(session, text="一二三四五", sizes=[5], step=120)
    original = token_times(session, version.id)

    align_service.apply_shift(session, version.id, shift_ms=-80)
    align_service.apply_shift(session, version.id, shift_ms=0)

    assert token_times(session, version.id) == original


def test_changing_the_offset_applies_only_the_difference(session: Session) -> None:
    version = make_version(session, text="一二三四", sizes=[4])
    original = token_times(session, version.id)

    align_service.apply_shift(session, version.id, shift_ms=-80)
    align_service.apply_shift(session, version.id, shift_ms=-50)

    shifted = token_times(session, version.id)
    assert all(o[0] - 50 == s[0] for o, s in zip(original, shifted, strict=True))


def test_deletions_shift_with_the_tokens(session: Session) -> None:
    """A cut must stay attached to the syllable it was made for.

    Shifting only the tokens would slide every deletion off the word it was
    meant to remove — the audio would be cut in the wrong place, silently.
    """
    version = make_version(session, text="一二三四", sizes=[4])
    add_deletion(session, version.id, 500, 600)

    result = align_service.apply_shift(session, version.id, shift_ms=-80)

    rows = session.query(Deletion).filter_by(version_id=version.id).all()
    assert [(r.start_ms, r.end_ms) for r in rows] == [(420, 520)]
    assert result.deletions == 1


# --- clamping -----------------------------------------------------------


def test_shift_is_clamped_at_zero_not_truncated(session: Session) -> None:
    """A file whose first word is at 60 ms cannot move 80 ms earlier.

    The *delta* is reduced so the whole timeline stays rigid. Clamping rows
    individually would squeeze the head of the recording and quietly change the
    timing of everything after the first word.
    """
    version = seed_version(session, text="一二", sizes=[2], step=30)  # first token at 0
    with_lead(session, version.id, 60)

    result = align_service.apply_shift(session, version.id, shift_ms=-80)

    assert result.applied_ms == -60
    assert result.clamped
    assert "负数" in result.reason
    times = token_times(session, version.id)
    assert min(t[0] for t in times) == 0
    # The gap between the two tokens is unchanged — the axis stayed rigid.
    assert times[1][0] - times[0][0] == 30


def test_shift_rejected_outside_the_sane_range(session: Session) -> None:
    version = make_version(session, text="一二", sizes=[2])
    with pytest.raises(align_service.AlignError) as caught:
        align_service.apply_shift(session, version.id, shift_ms=-5000)
    assert caught.value.code == "time_shift_out_of_range"


def test_shift_on_missing_version_is_not_found(session: Session) -> None:
    with pytest.raises(align_service.AlignError) as caught:
        align_service.apply_shift(session, "ver_nope", shift_ms=-80)
    assert caught.value.code == "version_not_found"


# --- interaction with human edits --------------------------------------


def test_human_edge_drag_keeps_its_relative_position(session: Session) -> None:
    """Apply the *difference*, so a hand-corrected token is not reset.

    Recomputing every timestamp from the raw ASR response would be simpler, and
    it would silently throw away every edge the user dragged. The drag is stored
    as a new absolute time, so only a delta keeps it meaningful.
    """
    version = make_version(session, text="一二三四", sizes=[4])
    # The user drags token 2's start 40 ms later than the ASR said.
    dragged = session.query(Token).filter_by(version_id=version.id, idx=2).one()
    dragged.start_ms += 40
    session.flush()
    expected = dragged.start_ms

    align_service.apply_shift(session, version.id, shift_ms=-80)

    session.expire_all()
    moved = session.query(Token).filter_by(version_id=version.id, idx=2).one()
    assert moved.start_ms == expected - 80


# --- HTTP ---------------------------------------------------------------


def test_endpoint_applies_and_reports(client: TestClient) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = make_version(session, text="一二三四", sizes=[4])
        version_id = version.id

    response = client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -80})
    assert response.status_code == 200
    body = response.json()
    assert body["shift_ms"] == -80
    assert body["previous_ms"] == 0
    assert body["delta_ms"] == -80
    assert body["tokens"] == 4
    assert body["clamped"] is False


def test_endpoint_reports_the_state(client: TestClient) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = make_version(session, text="一二三四", sizes=[4])
        version_id = version.id

    client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -80})
    body = client.get(f"/api/versions/{version_id}/time-shift").json()
    assert body["time_shift_ms"] == -80
    assert body["earliest_ms"] == 320


def test_endpoint_rejects_out_of_range_with_a_hint(client: TestClient) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = make_version(session, text="一二", sizes=[2])
        version_id = version.id

    response = client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": 99999})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "time_shift_out_of_range"
    assert error["hint"]


def test_version_payload_exposes_the_current_offset(client: TestClient) -> None:
    """The editor shows 「已对齐到 −80ms」 from the version payload, no extra call."""
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = make_version(session, text="一二三四", sizes=[4])
        version_id = version.id
        project_id = version.project_id

    client.post(f"/api/versions/{version_id}/time-shift", json={"shift_ms": -80})
    rows = client.get(f"/api/projects/{project_id}/versions").json()
    assert [row["time_shift_ms"] for row in rows] == [-80]
