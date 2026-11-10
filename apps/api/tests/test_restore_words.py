"""Word-scoped undo of a deletion — the editor's 「还」 button.

The rule this encodes: restoring a word must not restore its neighbours. A
sentence delete is one row covering forty words; the button is pressed on one of
them, and bringing back the other thirty-nine is not what was asked for.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import transcribe as transcribe_service


def seed(session: Session, **kwargs: object) -> str:
    version = seed_version(session, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return version.id


def test_restoring_one_word_leaves_the_rest_cut(client: TestClient, session: Session) -> None:
    """The whole point: a sentence delete is one row, one word comes back."""
    version_id = seed(session, text="剧集标签很好用", sizes=[7])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": list(range(7))})

    response = client.post(
        f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [3]}
    )
    assert response.status_code == 200, response.text

    states = client.get(f"/api/versions/{version_id}/deletions").json()
    covered: set[int] = set()
    for row in states:
        covered.update(range(row["start_ms"] // 100, row["end_ms"] // 100))
    assert 3 not in covered, "the clicked word is audible again"
    assert {0, 1, 2, 4, 5, 6} <= covered, "its neighbours must stay cut"


def test_restoring_splits_a_row_into_two(client: TestClient, session: Session) -> None:
    """One row becomes two: before the word, and after it."""
    version_id = seed(session, text="剧集标签很好用", sizes=[7])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": list(range(7))})
    client.post(f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [3]})
    rows = client.get(f"/api/versions/{version_id}/deletions").json()
    assert len(rows) == 2


def test_restoring_the_whole_selection_leaves_no_rows(
    client: TestClient, session: Session
) -> None:
    version_id = seed(session, text="剧集标签很好用", sizes=[5])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": list(range(5))})
    client.post(
        f"/api/versions/{version_id}/deletions/restore", json={"token_ids": list(range(5))}
    )
    assert client.get(f"/api/versions/{version_id}/deletions").json() == []


def test_restoring_an_undeleted_word_is_a_no_op(client: TestClient, session: Session) -> None:
    """Pressing 还 on a word that was never deleted must not disturb anything."""
    version_id = seed(session, text="剧集标签很好用", sizes=[7])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": [1, 2]})
    before = client.get(f"/api/versions/{version_id}/deletions").json()

    response = client.post(
        f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [5]}
    )
    assert response.status_code == 200
    assert client.get(f"/api/versions/{version_id}/deletions").json() == before


def test_restoring_nothing_is_rejected(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="剧集标签很好用", sizes=[5])
    response = client.post(f"/api/versions/{version_id}/deletions/restore", json={"token_ids": []})
    assert response.status_code == 400
    assert "没有选中任何字" in response.text


def test_restoring_out_of_range_indices_is_rejected(
    client: TestClient, session: Session
) -> None:
    version_id = seed(session, text="剧集标签很好用", sizes=[5])
    response = client.post(
        f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [99]}
    )
    assert response.status_code == 400


def test_restoring_brings_the_audio_back_into_the_edl(
    client: TestClient, session: Session
) -> None:
    """The EDL is derived, so the restored word has to reappear in it."""
    version_id = seed(session, text="剧集标签很好用", sizes=[5])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": [2]})
    removed_before = client.get(f"/api/versions/{version_id}/edl").json()["removed_ms"]
    assert removed_before > 0

    client.post(f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [2]})
    removed_after = client.get(f"/api/versions/{version_id}/edl").json()["removed_ms"]
    assert removed_after == 0


def test_restoring_does_not_untouch_a_different_row(
    client: TestClient, session: Session
) -> None:
    """Two separate cuts: restoring a word in one must leave the other alone."""
    version_id = seed(session, text="剧集标签很好用", sizes=[7])
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": [0, 1]})
    client.post(f"/api/versions/{version_id}/deletions", json={"token_ids": [5, 6]})

    client.post(f"/api/versions/{version_id}/deletions/restore", json={"token_ids": [0]})
    rows = client.get(f"/api/versions/{version_id}/deletions").json()
    spans = {(row["start_ms"], row["end_ms"]) for row in rows}
    assert (500, 700) in spans, "the untouched cut must still cover tokens 5–6"
    assert len(rows) == 2


def test_hidden_words_are_not_deleted_words(client: TestClient, session: Session) -> None:
    """Hiding must not make 「还」 appear: the audio was never cut."""
    version_id = seed(session, text="剧集标签很好用", sizes=[5])
    client.post(f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 5, "hidden": True})
    assert client.get(f"/api/versions/{version_id}/deletions").json() == []
    _ = transcribe_service.load_tokens(session, version_id)
