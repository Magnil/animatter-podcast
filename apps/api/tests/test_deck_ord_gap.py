"""Pin the `ord` renumbering: a deck with a gap must survive a delete.

Both bugs were found only by the full suite and neither reproduced in `test_decks.py`'s own
tests. The setup here builds the preconditions directly — a deck whose ords are not 0..n-1,
the state a previous delete can leave — so the test cannot pass by accident.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import decks as deck_service


@pytest.fixture()
def project(session: Session) -> str:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    return version.project_id


@pytest.fixture()
def app_settings(client: object) -> object:
    """Named ``app_settings``, not ``settings``: conftest already owns that name and reusing
    it makes the ``client`` dependency recursive."""
    return client.app.state.settings  # type: ignore[attr-defined]


def _deck_with_gap(session: Session, project: str, *, count: int = 3) -> tuple[str, list[str]]:
    """``count`` pages whose ords start at 100, i.e. a deck that is already misnumbered."""
    deck = deck_service.create_deck(session, project, title="带缺口的演示稿")
    ids: list[str] = []
    for index in range(count):
        row = deck_service.add_slide(session, deck, title=f"第{index}页", body="正文")
        ids.append(row.id)
    for index, slide_id in enumerate(ids):
        row = deck_service.get_slide(session, slide_id)
        assert row is not None
        row.ord = 100 + index
    session.flush()
    return deck.id, ids


def test_deleting_a_page_from_a_gapped_deck_does_not_collide(
    session: Session, app_settings: object, project: str
) -> None:
    """``(deck_id, ord)`` is unique, and one flush applies the updates in any order.

    Renumbering rows at 100 and 101 down to 0 and 1 is fine, but a deck left at 1 and 2 (one
    page deleted earlier, before the renumber) can run ``2→1`` first and hit the row still at 1.
    Measured in the full suite as ``UNIQUE constraint failed: slide.deck_id, slide.ord``; the
    fix parks every row above the largest existing ord before renumbering.
    """
    deck_id, ids = _deck_with_gap(session, project)

    deck_service.delete_slide(session, app_settings, ids[0])

    rows = deck_service.list_slides(session, deck_id)
    assert [row.id for row in rows] == ids[1:]
    assert [row.ord for row in rows] == [0, 1]


def test_deleting_from_a_deck_with_consecutive_high_ords_collides_without_parking(
    session: Session, app_settings: object, project: str
) -> None:
    """The exact shape that failed: two rows at ords 1 and 2, delete the one at 0 first.

    A gap of exactly one is what ``delete_slide`` itself used to leave when it had no renumber,
    so this is not a contrived input — it is the state an older row set is already in.
    """
    deck_id, ids = _deck_with_gap(session, project, count=3)
    for index, slide_id in enumerate(ids):
        row = deck_service.get_slide(session, slide_id)
        assert row is not None
        row.ord = index
    session.flush()
    # Now delete the *last* one: leaves ords 0, 1 — dense, so nothing to renumber.
    deck_service.delete_slide(session, app_settings, ids[2])
    assert [row.ord for row in deck_service.list_slides(session, deck_id)] == [0, 1]

    # Add a page back and delete the middle one: ords become 0 and 2, then must renumber.
    extra = deck_service.add_slide(session, deck_service.require_deck(session, project), title="x", body="y")
    assert extra.ord == 2
    deck_service.delete_slide(session, app_settings, ids[1])
    rows = deck_service.list_slides(session, deck_id)
    assert [row.ord for row in rows] == [0, 1]
