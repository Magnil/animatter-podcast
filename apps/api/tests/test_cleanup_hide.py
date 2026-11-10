"""Batch-hiding filler words, and the two things it must not do.

Bulk hiding is a different operation from bulk deleting, and the differences that
matter are both negative:

* it must not create deletion rows (the audio is not touched — that is the whole
  point), and
* "还原已隐去口癖词" must not also un-hide words the user hid one at a time
  while reading, because those were deliberate decisions.

The tests below pin those two, plus the mutual exclusion the menu relies on:
after a batch hide, the filler finder must propose nothing to *delete*.

:func:`tests.test_corrections.seed_version` makes **one token per character**, so
«嗯这个很好» is five tokens.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import editing
from animatter.services import transcribe as transcribe_service


def seed(session: Session, **kwargs: object) -> str:
    version = seed_version(session, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return version.id


def hidden_indices(session: Session, version_id: str) -> set[int]:
    return {token.idx for token in transcribe_service.load_tokens(session, version_id) if token.hidden}


def test_hide_fillers_marks_words_without_cutting_audio(
    client: TestClient, session: Session
) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    before = [
        (t.idx, t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version_id)
    ]

    response = client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    assert response.status_code == 201, response.text
    assert response.json()["hidden"] >= 1

    session.expire_all()
    after = transcribe_service.load_tokens(session, version_id)
    # Every timestamp is exactly where it was: hiding is a text-only act.
    assert [(t.idx, t.start_ms, t.end_ms) for t in after] == before
    assert any(t.hidden for t in after)


def test_hide_fillers_reports_places_not_raw_words(
    client: TestClient, session: Session
) -> None:
    """The response must be in the unit the dialog counted.

    On the real 3-hour file the plan says 「173 处」while 183 tokens carry the
    hidden flag, because ten pairs of adjacent fillers («嗯嗯») merged into one
    row, one grey band. Reporting 183 makes the confirmation disagree with the
    button the user just pressed, which reads as a bug even though both numbers
    are individually right.
    """
    version_id = seed(session, text="嗯嗯这个很好", sizes=[6])
    body = client.post(f"/api/versions/{version_id}/cleanup/hide-fillers").json()
    # Two «嗯» sit next to each other: one place, one grey band, two tokens.
    assert body["hidden"] == 1, "adjacent fillers are one place"
    assert body["words"] == 2
    assert body["hidden"] <= body["words"]


def test_hide_fillers_creates_no_deletion_rows(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    edl = client.get(f"/api/versions/{version_id}/edl").json()
    assert edl["deletions"] == []


def test_hide_fillers_is_one_history_entry(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯啊哦这个很好", sizes=[7])
    client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    rows = client.get(f"/api/versions/{version_id}/history").json()
    hide_rows = [row for row in rows if row["op_type"] == "hide"]
    assert len(hide_rows) == 1
    assert hide_rows[0]["undoable"] is True
    assert "批量隐去" in hide_rows[0]["detail"]


def test_hide_fillers_then_the_filler_finder_proposes_nothing(
    client: TestClient, session: Session
) -> None:
    """After hiding, the delete plan must have nothing of this kind to offer.

    The two actions are mutually exclusive in the product sense: a word is either
    hidden or deleted. If the filler finder still proposed the hidden ones, the
    menu would offer to delete audio the user had just decided to keep.
    """
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    plan = client.get(f"/api/versions/{version_id}/cleanup/plan").json()
    assert plan["summary"]["filler"]["items"] == 0


def test_restoring_hidden_fillers_leaves_manual_hides_alone(
    client: TestClient, session: Session
) -> None:
    """The whole reason ``hide_source`` exists."""
    version_id = seed(session, text="嗯这个很好啊", sizes=[6])
    # Hide the last word by hand — the same service call the editor's Backspace
    # makes.
    editing.apply_hide(session, version_id, token_start=5, token_end=6)
    session.commit()

    client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    session.expire_all()
    assert 5 in hidden_indices(session, version_id)

    response = client.post(f"/api/versions/{version_id}/cleanup/restore?source=hidden_filler")
    assert response.status_code == 200, response.text
    assert response.json()["restored"] >= 1

    session.expire_all()
    # Only the hand-hidden word is left. This is the assertion that fails if the
    # restore path decides to "helpfully" clear every hidden flag.
    assert hidden_indices(session, version_id) == {5}


def test_restoring_hidden_fillers_counts_them(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    before = client.get(f"/api/versions/{version_id}/cleanup/counts").json()["counts"]
    assert before["hidden_filler"] == 0

    hidden = client.post(f"/api/versions/{version_id}/cleanup/hide-fillers").json()
    counts = client.get(f"/api/versions/{version_id}/cleanup/counts").json()["counts"]
    # The menu count and the confirmation have to be the same number. They were
    # not when counts_by_kind counted tokens and hide_fillers counted places.
    assert counts["hidden_filler"] == hidden["hidden"]

    restored = client.post(f"/api/versions/{version_id}/cleanup/restore?source=hidden_filler").json()
    assert restored["restored"] == hidden["hidden"]


def test_hide_fillers_with_nothing_left_is_a_clear_error(
    client: TestClient, session: Session
) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    second = client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    assert second.status_code == 400
    assert "没有可隐去的口癖词" in second.text


def test_deleting_then_hiding_cannot_both_apply(client: TestClient, session: Session) -> None:
    """The pairing the menu disables, asserted at the API level too.

    The menu greys one out, but a stale tab can still post both; the server has to
    refuse on its own.
    """
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    deleted = client.post(f"/api/versions/{version_id}/cleanup", json={"kinds": ["filler"]})
    assert deleted.status_code == 201

    response = client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    assert response.status_code == 400


@pytest.mark.parametrize("hide_first", [False, True])
def test_counts_always_report_hidden_filler(
    client: TestClient, session: Session, hide_first: bool
) -> None:
    """The menu reads this key unconditionally; a missing key would render "undefined"."""
    version_id = seed(session, text="这个很好", sizes=[4])
    if hide_first:
        client.post(f"/api/versions/{version_id}/cleanup/hide-fillers")
    counts = client.get(f"/api/versions/{version_id}/cleanup/counts").json()["counts"]
    assert "hidden_filler" in counts
