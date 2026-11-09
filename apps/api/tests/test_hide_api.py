"""The HTTP surface of hiding, and multi-word correction through the same path.

Two things are asserted here that the service tests cannot: that the endpoint shape
is what the editor calls, and that a *multi-word* selection collapses into one
corrected token with the window's outer edges intact — the "drag over four words
and rewrite them as one" case.

Seeding goes through the ``session`` fixture and commits: the HTTP client reads
through its own session, so uncommitted rows are invisible to it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import subtitles
from animatter.services import transcribe as transcribe_service


def seed(session: Session, **kwargs: object) -> str:
    version = seed_version(session, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return version.id


def test_hide_endpoint_marks_words_and_leaves_timing(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    response = client.post(
        f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["hidden"] is True
    assert response.json()["changed"] == 1

    transcript = client.get(f"/api/versions/{version_id}/transcript").json()
    assert [token["hidden"] for token in transcript["tokens"]] == [True, False, False, False, False]
    # The audio is untouched, so every timestamp is exactly where it was.
    assert transcript["tokens"][0]["start_ms"] == 0
    assert transcript["tokens"][0]["end_ms"] == 100


def test_hide_endpoint_un_hides(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这", sizes=[2])
    client.post(f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 1})
    response = client.post(
        f"/api/versions/{version_id}/hide",
        json={"token_start": 0, "token_end": 1, "hidden": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["hidden"] is False
    transcript = client.get(f"/api/versions/{version_id}/transcript").json()
    assert transcript["tokens"][0]["hidden"] is False


def test_hidden_words_disappear_from_exported_subtitles(
    client: TestClient, session: Session
) -> None:
    version_id = seed(session, text="嗯这个很好", sizes=[5])
    client.post(f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 1})
    session.expire_all()
    cues, _ = subtitles.build_cues(session, version_id, time_map=None)
    assert cues[0].text == "这个很好"


def test_multi_word_correction_merges_into_one_token(
    client: TestClient, session: Session
) -> None:
    """Dragging over four words and writing one replacement is supported.

    The invariant is that the *outer* boundaries do not move: the merged token
    starts where the first word started and ends where the last word ended, so
    the surrounding words keep their absolute positions.
    """
    version_id = seed(session, text="剧集标签好", sizes=[5])
    before = transcribe_service.load_tokens(session, version_id)

    response = client.post(
        f"/api/versions/{version_id}/text",
        json={"token_start": 0, "token_end": 4, "proposed": "句级标签"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["delta"] == -3

    session.expire_all()
    after = transcribe_service.load_tokens(session, version_id)
    assert [t.text for t in after] == ["句级标签", "好"]
    assert (after[0].start_ms, after[0].end_ms) == (before[0].start_ms, before[3].end_ms)
    # The untouched word after the window has not moved.
    assert (after[1].start_ms, after[1].end_ms) == (before[4].start_ms, before[4].end_ms)
    assert after[0].revised is True


def test_multi_word_correction_rewrites_the_sentence_range(
    client: TestClient, session: Session
) -> None:
    """A shrunk token stream must still tile: sentence ranges shift with it."""
    version_id = seed(session, text="剧集标签好", sizes=[5])
    client.post(
        f"/api/versions/{version_id}/text",
        json={"token_start": 0, "token_end": 4, "proposed": "句级标签"},
    )
    session.expire_all()
    spans = transcribe_service.load_sentences(session, version_id)
    tokens = transcribe_service.load_tokens(session, version_id)
    assert spans[0].token_end == len(tokens) == 2


def test_multi_word_correction_renumbers_without_colliding(
    client: TestClient, session: Session
) -> None:
    """Shrinking by more than one used to hit the UNIQUE (version_id, idx).

    A real transcript failed with ``UNIQUE constraint failed: token.version_id,
    token.idx`` when merging four words into one: shifting the tail down by 3
    makes row 5 want index 2 while row 2 still occupies it, and the order the
    unit of work emits those UPDATEs in is not controlled. The fix parks the tail
    above every live index first.

    The assertion is just that many rows survive in the right order — the failure
    mode was a hard 500, so "it returned 200 and the text is right" is the test.
    """
    version_id = seed(session, text="一二三四五六七八九十", sizes=[10])
    before = transcribe_service.load_tokens(session, version_id)
    response = client.post(
        f"/api/versions/{version_id}/text",
        json={"token_start": 2, "token_end": 6, "proposed": "合并"},
    )
    assert response.status_code == 200, response.text

    session.expire_all()
    after = transcribe_service.load_tokens(session, version_id)
    assert [t.text for t in after] == ["一", "二", "合并", "七", "八", "九", "十"]
    # Dense, ascending, and no index reused.
    assert [t.idx for t in after] == list(range(len(after)))
    # The words after the window kept their absolute times.
    assert [(t.start_ms, t.end_ms) for t in after[3:]] == [
        (t.start_ms, t.end_ms) for t in before[6:]
    ]


def test_correcting_a_hidden_word_un_hides_it(client: TestClient, session: Session) -> None:
    """Rewriting is a decision about what the word says, so the word comes back."""
    version_id = seed(session, text="嗯这", sizes=[2])
    client.post(f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 1})
    client.post(
        f"/api/versions/{version_id}/text",
        json={"token_start": 0, "token_end": 1, "proposed": "诶"},
    )
    session.expire_all()
    token = transcribe_service.load_tokens(session, version_id)[0]
    assert token.text == "诶"
    assert token.hidden is False


def test_hide_is_rejected_for_a_bad_range(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这", sizes=[2])
    response = client.post(
        f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 99}
    )
    assert response.status_code == 400
    assert "超出范围" in response.text


def test_history_reports_hide_as_undoable(client: TestClient, session: Session) -> None:
    version_id = seed(session, text="嗯这", sizes=[2])
    client.post(f"/api/versions/{version_id}/hide", json={"token_start": 0, "token_end": 1})
    rows = client.get(f"/api/versions/{version_id}/history").json()
    assert rows[0]["op_type"] == "hide"
    assert rows[0]["undoable"] is True


@pytest.mark.parametrize("payload", [{"token_start": 0, "token_end": 1, "hidden": "maybe"}])
def test_hide_rejects_a_value_that_is_not_a_boolean(
    client: TestClient, session: Session, payload: dict[str, object]
) -> None:
    # "no"/"yes" are coerced by pydantic and are fine; "maybe" is not a boolean at
    # all, and silently reading it as False would un-hide words on a typo.
    version_id = seed(session, text="嗯这", sizes=[2])
    response = client.post(f"/api/versions/{version_id}/hide", json=payload)
    assert response.status_code == 422
