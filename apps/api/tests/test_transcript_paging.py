"""Transcript paging: the server pages, so the client must page too.

The bug this file pins down, reported against a real 105-minute episode:

    「1 小时 45 分的音频只转写了 40 分钟半（1074 句 · 11582 词），后续 1 小时都
    没有转写。」

Nothing was missing from the database. The route returns at most ``limit``
sentences and the client asked for one page of 400, so the list stopped at 40
minutes while the status bar — reading ``sentence_count`` straight from the
version row — said 1074. Two numbers, both correct, describing different things.
The app looked like transcription had died halfway.

So: the page really is bounded, ``from_sentence`` really does continue, the
bounds compose into the whole transcript, and ``sentence_count`` really does
describe the whole thing. If any of those stops being true the client's paging
loop silently truncates again.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.services import editing


def _seeded(client: TestClient, session: Session, sentences: int = 12):  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    text = "一二三四五六七八九十" * 3
    version = seed_version(session, text=text, sizes=[30])
    # Split into `sentences` chunks so paging has something to page through.
    step = 30 // sentences
    for index in range(1, sentences):
        editing.apply_boundary(
            session, version.id, action="new_sentence", from_token=index * step, to_token=index * step
        )
    session.commit()
    return version


def _seeded_long(client: TestClient, session: Session):  # type: ignore[no-untyped-def]
    """520 one-token sentences: more than the server's 500-sentence cap."""
    from tests.test_corrections import seed_version

    version = seed_version(session, text="字" * 520, sizes=[520])
    for index in range(1, 520):
        editing.apply_boundary(
            session, version.id, action="new_sentence", from_token=index, to_token=index
        )
    session.commit()
    return version


def test_the_route_returns_only_the_requested_window(client: TestClient, session: Session) -> None:
    version = _seeded(client, session)
    payload = client.get(f"/api/versions/{version.id}/transcript?from_sentence=0&limit=5").json()

    assert len(payload["sentences"]) == 5
    assert payload["sentences"][0]["ord"] == 0
    # The count is the whole transcript, not the window: that difference is what
    # made the header and the list disagree.
    assert payload["version"]["sentence_count"] == 12


def test_from_sentence_continues_where_the_last_page_stopped(
    client: TestClient, session: Session
) -> None:
    version = _seeded(client, session)
    page1 = client.get(f"/api/versions/{version.id}/transcript?from_sentence=0&limit=5").json()
    page2 = client.get(f"/api/versions/{version.id}/transcript?from_sentence=5&limit=5").json()

    assert [s["ord"] for s in page2["sentences"]] == [5, 6, 7, 8, 9]
    assert page1["sentences"][-1]["ord"] == 4
    # Tokens are windowed too, so pages must not overlap or leave a gap.
    assert page1["tokens"][-1]["idx"] + 1 == page2["tokens"][0]["idx"]


def test_paging_covers_the_whole_transcript(client: TestClient, session: Session) -> None:
    """What the client's paging loop is supposed to produce."""
    version = _seeded(client, session)
    total = client.get(f"/api/versions/{version.id}/transcript?limit=1").json()["version"][
        "sentence_count"
    ]

    ords: list[int] = []
    tokens: list[int] = []
    from_sentence = 0
    while from_sentence < total:
        page = client.get(
            f"/api/versions/{version.id}/transcript?from_sentence={from_sentence}&limit=5"
        ).json()
        assert page["sentences"], f"page at {from_sentence} came back empty"
        ords.extend(s["ord"] for s in page["sentences"])
        tokens.extend(t["idx"] for t in page["tokens"])
        from_sentence += len(page["sentences"])

    assert ords == list(range(total))
    assert tokens == list(range(len(tokens)))


def test_limit_is_capped_server_side(client: TestClient, session: Session) -> None:
    """A client asking for everything must not get a multi-megabyte response.

    The cap is the reason a client *has* to page; it is also why asking for
    ``limit=100000`` cannot be the fix for a truncated list.
    """
    version = _seeded_long(client, session)
    payload = client.get(f"/api/versions/{version.id}/transcript?from_sentence=0&limit=100000").json()

    assert payload["version"]["sentence_count"] == 520
    assert len(payload["sentences"]) == 500
