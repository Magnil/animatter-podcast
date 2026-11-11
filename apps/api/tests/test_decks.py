"""Deck and slide storage, including the ordering rules (P4, §19.6).

Two things about the fixtures. First, a page is now a **minute of speech**, so a fixture that
wants "the second page" has to build sentences that are actually a minute long — a 4-second
sentence is a page-fragment and gets merged. Second, three rules here cannot be seen from the
service's signature and all three break the UI:

* **``ord`` must stay dense.** A gap is invisible in a list query but makes every later
  "move up/down" off by one.
* **Reordering must not violate the unique ``(deck_id, ord)`` index.** A direct swap writes a
  duplicate mid-statement and SQLite rejects it.
* **The PNG must be invalidated when the text changes.** Otherwise the editor saves, the
  preview keeps showing the old page, and the only way to fix it is to delete the deck.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.script import Chapter, ScriptSentence
from animatter.errors import AppError
from animatter.services import decks as deck_service

#: Length of a fixture sentence, in ms. Comfortably past ``TARGET_PAGE_MS`` (60 s), so each
#: sentence is its own page and a test asking for "page 2" gets one.
SPAN = 70_000
#: A pause between fixture sentences: past ``PAGE_GAP_MS`` (700 ms), so the break is a seam.
GAP = 800


def sentence(ord_: int, start: int, end: int, text: str, speaker: int | None = 0) -> ScriptSentence:
    return ScriptSentence(ord=ord_, start_ms=start, end_ms=end, speaker=speaker, text=text)


def page(ord_: int, text: str, *, at: int | None = None) -> ScriptSentence:
    """One sentence long enough to be its own page."""
    start = ord_ * (SPAN + GAP) if at is None else at
    return sentence(ord_, start, start + SPAN, text)


def pages(count: int, *, first_ord: int = 0, start: int = 0) -> list[ScriptSentence]:
    out: list[ScriptSentence] = []
    cursor = start
    for index in range(count):
        out.append(sentence(first_ord + index, cursor, cursor + SPAN, f"第{index}页的内容。"))
        cursor += SPAN + GAP
    return out


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


# --- deck lifecycle -----------------------------------------------------


def test_a_project_starts_with_no_deck(session: Session, project: str) -> None:
    assert deck_service.get_deck(session, project) is None


def test_requiring_a_missing_deck_is_a_404_with_a_hint(
    session: Session, project: str
) -> None:
    with pytest.raises(AppError) as caught:
        deck_service.require_deck(session, project)
    assert caught.value.status == 404
    assert "生成" in caught.value.hint


def test_creating_twice_returns_the_same_deck(session: Session, project: str) -> None:
    """Two decks would need a chooser on every screen and a rule for what the box plays."""
    first = deck_service.create_deck(session, project, title="第一期")
    second = deck_service.create_deck(session, project, title="别的名字")
    assert first.id == second.id
    assert second.title == "第一期", "the existing deck's title must not be overwritten"


def test_a_deck_without_a_title_gets_one(session: Session, project: str) -> None:
    deck = deck_service.create_deck(session, project)
    assert deck.title == deck_service.DEFAULT_TITLE


# --- generation ---------------------------------------------------------


def test_generation_makes_one_page_per_topic_stretch(
    session: Session, app_settings: object, project: str
) -> None:
    deck, slides = deck_service.generate_from_transcript(
        session,
        app_settings,
        project,
        sentences=[page(0, "第一段内容。"), page(1, "第二段内容。")],
        total_ms=2 * (SPAN + GAP),
    )
    assert len(slides) == 2
    assert [view.row.title for view in slides] == ["第一段内容", "第二段内容"]
    assert deck.project_id == project


def test_generation_replaces_the_previous_pages(
    session: Session, app_settings: object, project: str
) -> None:
    """「重新生成」 is the operation people want; accumulating duplicates is not."""
    deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    _deck, slides = deck_service.generate_from_transcript(
        session,
        app_settings,
        project,
        sentences=[page(0, "乙。"), page(1, "丙。")],
        total_ms=2 * (SPAN + GAP),
    )
    assert len(slides) == 2


def test_generation_can_append_instead_of_replacing(
    session: Session, app_settings: object, project: str
) -> None:
    deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    _deck, slides = deck_service.generate_from_transcript(
        session,
        app_settings,
        project,
        sentences=[page(1, "乙。")],
        total_ms=SPAN,
        replace=False,
    )
    assert len(slides) == 2
    assert [view.row.ord for view in slides] == [0, 1]


def test_a_chapter_opening_page_takes_the_chapter_heading(
    session: Session, app_settings: object, project: str
) -> None:
    """A chapter heading names the stretch starting there — it is not a summary of a page."""
    second = page(5, "第二个话题的内容。")
    _deck, slides = deck_service.generate_from_transcript(
        session,
        app_settings,
        project,
        sentences=[page(0, "开场白内容。"), second],
        chapters=[
            Chapter(heading="第二章 长片与短片", start_ms=second.start_ms, first_ord=5)
        ],
        total_ms=2 * (SPAN + GAP),
    )
    assert slides[1].row.title == "第二章 长片与短片"
    assert slides[0].row.title != "第二章 长片与短片"


def test_an_empty_transcript_makes_no_pages(
    session: Session, app_settings: object, project: str
) -> None:
    _deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=[], total_ms=0
    )
    assert slides == []


# --- editing ------------------------------------------------------------


def test_editing_a_page_updates_it_in_place(
    session: Session, app_settings: object, project: str
) -> None:
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    row = deck_service.update_slide(
        session, slides[0].row.id, title="改过的标题", body="改过的正文"
    )
    assert (row.title, row.body) == ("改过的标题", "改过的正文")
    assert row.deck_id == deck.id


def test_an_unknown_layout_is_refused(
    session: Session, app_settings: object, project: str
) -> None:
    _deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    with pytest.raises(AppError) as caught:
        deck_service.update_slide(session, slides[0].row.id, layout="huge")
    assert "版式" in caught.value.message


def test_editing_an_unknown_slide_is_a_404(session: Session) -> None:
    with pytest.raises(AppError) as caught:
        deck_service.update_slide(session, "sld_missing", title="x")
    assert caught.value.status == 404


# --- ordering -----------------------------------------------------------


def _three(session: Session, app_settings: object, project: str) -> list[str]:
    _deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(3), total_ms=3 * (SPAN + GAP)
    )
    assert len(slides) == 3, "the fixture must produce three pages or moving proves nothing"
    return [view.row.id for view in slides]


def test_moving_a_page_forward_reorders_the_rest(
    session: Session, app_settings: object, project: str
) -> None:
    ids = _three(session, app_settings, project)
    rows = deck_service.move_slide(session, ids[0], to_ord=2)
    assert [row.id for row in rows] == [ids[1], ids[2], ids[0]]
    assert [row.ord for row in rows] == [0, 1, 2]


def test_moving_a_page_backward_reorders_the_rest(
    session: Session, app_settings: object, project: str
) -> None:
    ids = _three(session, app_settings, project)
    rows = deck_service.move_slide(session, ids[2], to_ord=0)
    assert [row.id for row in rows] == [ids[2], ids[0], ids[1]]


def test_moving_to_the_same_position_changes_nothing(
    session: Session, app_settings: object, project: str
) -> None:
    ids = _three(session, app_settings, project)
    rows = deck_service.move_slide(session, ids[1], to_ord=1)
    assert [row.id for row in rows] == ids


def test_moving_past_the_end_clamps(session: Session, app_settings: object, project: str) -> None:
    ids = _three(session, app_settings, project)
    rows = deck_service.move_slide(session, ids[0], to_ord=99)
    assert [row.id for row in rows] == [ids[1], ids[2], ids[0]]


def test_deleting_keeps_the_order_dense(
    session: Session, app_settings: object, project: str
) -> None:
    """A gap in ``ord`` makes every later move up/down off by one, invisibly."""
    ids = _three(session, app_settings, project)
    deck_service.delete_slide(session, app_settings, ids[1])
    deck = deck_service.get_deck(session, project)
    assert deck is not None
    rows = deck_service.list_slides(session, deck.id)
    assert [row.id for row in rows] == [ids[0], ids[2]]
    assert [row.ord for row in rows] == [0, 1]


def test_deleting_a_page_deletes_its_picture(
    session: Session, app_settings: object, project: str
) -> None:
    """The row is the only thing that knows the file's name, so it must go with the row.

    Left behind, the PNG is unreachable forever: nothing else records the slide id, and the
    next regenerate mints new ids.
    """
    ids = _three(session, app_settings, project)
    deck = deck_service.get_deck(session, project)
    assert deck is not None
    deck_service.render_slide(
        app_settings, project, deck.id, deck_service.list_slides(session, deck.id)[0],
        width=320, height=180,
    )
    before = list(deck_service.slides_dir(app_settings, project, deck.id).glob("*.png"))
    assert before, "the fixture must have drawn at least one page"

    deck_service.delete_slide(session, app_settings, ids[0])
    after = list(deck_service.slides_dir(app_settings, project, deck.id).glob("*.png"))
    assert not [path for path in after if path.name.startswith(f"{ids[0]}-")]


def test_deleting_an_unknown_slide_is_a_no_op(
    session: Session, app_settings: object
) -> None:
    """Idempotent on purpose: a double-click must not 404 after the first one succeeded."""
    deck_service.delete_slide(session, app_settings, "sld_missing")


def test_regenerating_does_not_leave_orphaned_pictures(
    session: Session, app_settings: object, project: str
) -> None:
    """「重新生成」 mints new ids, so every old PNG becomes garbage the moment the rows go.

    Measured before the fix: one regenerate on a 60-page deck left 62 orphaned files
    (14 MB) that nothing could ever reach again.
    """
    deck, first = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(3), total_ms=3 * (SPAN + GAP)
    )
    for view in first:
        deck_service.render_slide(
            app_settings, project, deck.id, view.row, width=320, height=180
        )
    drawn = list(deck_service.slides_dir(app_settings, project, deck.id).glob("*.png"))
    assert len(drawn) == 3

    _deck, second = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(3), total_ms=3 * (SPAN + GAP)
    )
    assert {view.row.id for view in second}.isdisjoint({view.row.id for view in first})
    assert not list(
        deck_service.slides_dir(app_settings, project, deck.id).glob("*.png")
    ), "the previous generation's pictures must be gone"


# --- rendering ---------------------------------------------------------


def test_a_slide_renders_at_the_box_size(
    session: Session, app_settings: object, project: str
) -> None:
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    path = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=640, height=360
    )
    assert path.is_file()
    assert Image.open(path).size == (640, 360)


def test_rendering_is_idempotent(
    session: Session, app_settings: object, project: str
) -> None:
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    first = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    stamp = first.stat().st_mtime_ns
    second = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    assert second == first
    assert second.stat().st_mtime_ns == stamp, "an unchanged page must not be redrawn"


def test_the_same_page_drawn_at_a_different_size_is_a_different_file(
    session: Session, app_settings: object, project: str
) -> None:
    """Caching by slide id alone served a 1267x712 PNG to a 1440x810 box.

    The overlay then upscaled it — which is exactly the soft text this design exists to
    avoid — and the body visibly ran past the box's right edge. The size has to be part of
    the file name.
    """
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    small = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    large = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=640, height=360
    )
    assert small != large
    assert Image.open(small).size == (320, 180)
    assert Image.open(large).size == (640, 360)


def test_invalidating_clears_every_size(
    session: Session, app_settings: object, project: str
) -> None:
    """Leaving the other size behind would keep showing the previous text there."""
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=640, height=360
    )
    deck_service.invalidate(app_settings, project, deck.id, slides[0].row.id)
    assert not list(
        deck_service.slides_dir(app_settings, project, deck.id).glob("*.png")
    )


def test_invalidating_forces_a_redraw(
    session: Session, app_settings: object, project: str
) -> None:
    """Without this, editing text leaves the preview showing the old page forever."""
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    first = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    before = first.read_bytes()

    deck_service.update_slide(session, slides[0].row.id, title="全新的标题")
    deck_service.invalidate(app_settings, project, deck.id, slides[0].row.id)
    deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, width=320, height=180
    )
    assert first.read_bytes() != before


def test_the_view_hands_out_a_usable_image_url(
    session: Session, app_settings: object, project: str
) -> None:
    """The URL is handed out before the PNG exists: the image route draws on demand.

    Returning ``""`` until something else rendered the page would make the UI depend on
    which request happened to run first — a bug that only appears on a cold project.
    """
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(1), total_ms=SPAN
    )
    view_pair = deck_service.view(session, project)
    assert view_pair is not None
    url = view_pair[1][0].as_dict(project)["image_url"]
    assert url.startswith(f"/api/projects/{project}/decks/{deck.id}/slides/")
    assert url.endswith("/image")
    assert not list(
        deck_service.slides_dir(app_settings, project, deck.id).glob("*.png")
    ), "the view must not have rendered anything"


# --- spans --------------------------------------------------------------


def test_spans_run_until_the_next_page(
    session: Session, app_settings: object, project: str
) -> None:
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=pages(2), total_ms=2 * (SPAN + GAP)
    )
    rows = deck_service.list_slides(session, deck.id)
    spans = deck_service.spans_of(rows, total_ms=2 * (SPAN + GAP))
    assert spans[0][1:] == (0, rows[1].start_ms)
    assert spans[1][1:] == (rows[1].start_ms, 2 * (SPAN + GAP))


def test_a_slide_png_is_keyed_by_id_not_by_title(
    session: Session, app_settings: object, project: str
) -> None:
    """Two pages with the same title must not share a file."""
    deck, slides = deck_service.generate_from_transcript(
        session,
        app_settings,
        project,
        sentences=[sentence(0, 0, SPAN, "同样。"), sentence(1, SPAN + GAP, 2 * SPAN, "同样。")],
        total_ms=2 * (SPAN + GAP),
    )
    a = Path(
        deck_service.render_slide(
            app_settings, project, deck.id, slides[0].row, width=320, height=180
        )
    )
    b = Path(
        deck_service.render_slide(
            app_settings, project, deck.id, slides[1].row, width=320, height=180
        )
    )
    assert a != b
    assert a.is_file() and b.is_file()
