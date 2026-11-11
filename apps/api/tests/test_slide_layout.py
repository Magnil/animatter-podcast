"""Slide text layout (§19.6).

The fake measurement here is the point: with "every character is 1 em" the expected line
breaks are arithmetically obvious, so a test failure names a real rule instead of a font
metric. The real renderer's measurement is ``ImageFont.getlength``, which needs a font file
and returns platform-dependent numbers.

The behaviours worth pinning are the four ways a generated deck looks broken:

* text overflowing the page (nobody wrapped it);
* text silently truncated (it wrapped, then something dropped the overflow);
* a line starting with 「，」 (the break ignored punctuation);
* the same input producing different pixels (auto-shrink that depends on iteration order).
"""

from __future__ import annotations

import pytest

from animatter.core.slide import (
    MAX_TITLE_PX,
    MIN_BODY_PX,
    MIN_TITLE_PX,
    NO_LINE_END,
    NO_LINE_START,
    layout,
    wrap,
)


def em(text: str, size: int) -> float:
    """Every character is ``size`` px wide — a font where the arithmetic is visible."""
    return len(text) * size


def measure(text: str, size: int) -> float:
    return em(text, size)


def _EM(text: str) -> float:
    """The same fake font, bound to 10 px so ``wrap`` can take it directly."""
    return em(text, 10)


def _em_width(chars: int, size: int) -> int:
    """The width of ``chars`` characters — how the expected break is spelled out."""
    return chars * size


# --- wrapping -----------------------------------------------------------


def test_a_short_line_does_not_wrap() -> None:
    assert wrap("短句", max_width=100, measure=lambda t: em(t, 10)) == ["短句"]


def test_cjk_wraps_by_width_not_by_space() -> None:
    """There are no spaces to break at, which is the whole reason this exists."""
    lines = wrap("一二三四五六", max_width=30, measure=lambda t: em(t, 10))
    assert lines == ["一二三", "四五六"]


def test_a_space_is_not_carried_to_the_next_line() -> None:
    lines = wrap("ab cd", max_width=30, measure=lambda t: em(t, 10))
    assert lines == ["ab", "cd"]


def test_a_character_wider_than_the_line_still_appears() -> None:
    """Dropping it would be the one unacceptable outcome: the text is unrecoverable."""
    lines = wrap("一二", max_width=5, measure=lambda t: em(t, 10))
    assert "".join(lines) == "一二"


def test_every_character_survives_wrapping() -> None:
    text = "这是一段没有任何标点的长中文用来验证折行不会丢字" * 3
    lines = wrap(text, max_width=100, measure=lambda t: em(t, 20))
    assert "".join(lines) == text


def test_explicit_newlines_start_a_new_line() -> None:
    lines = wrap("第一行\n第二行", max_width=1000, measure=lambda t: em(t, 10))
    assert lines == ["第一行", "第二行"]


def test_blank_paragraphs_are_skipped() -> None:
    lines = wrap("甲\n\n\n乙", max_width=1000, measure=lambda t: em(t, 10))
    assert lines == ["甲", "乙"]


def test_empty_text_gives_one_empty_line() -> None:
    assert wrap("", max_width=100, measure=lambda t: em(t, 10)) == [""]


# --- punctuation --------------------------------------------------------


def test_a_line_never_starts_with_a_closing_mark() -> None:
    """A break before 「，」 reads as a typo, so the mark hangs on the previous line.

    The old implementation wrapped by width and then merged whole lines that started with
    punctuation, which is how a real page ended up with a line 2.3× the box width — the ink
    touched the last pixel column. This test pins the *bounded* behaviour: one character of
    overflow, not one line of it.
    """
    lines = wrap("甲" * 10 + "，世界", max_width=_em_width(10, 10), measure=_EM)
    assert lines[0] == "甲" * 10 + "，"
    assert all(line[0] not in NO_LINE_START for line in lines[1:])


def test_a_long_run_of_marks_is_absorbed_rather_than_starting_a_line() -> None:
    lines = wrap("甲" * 10 + "，。；乙", max_width=_em_width(10, 10), measure=_EM)
    assert lines[0] == "甲" * 10 + "，。；"
    assert lines[1] == "乙"


def test_a_break_prefers_a_clause_mark_over_a_mid_phrase_cut() -> None:
    """Cutting after 「，」 reads better than cutting one character later."""
    lines = wrap("甲乙丙丁戊己，庚辛", max_width=_em_width(8, 10), measure=_EM)
    assert lines[0] == "甲乙丙丁戊己，"


def test_a_mark_far_from_the_edge_is_not_used() -> None:
    """Cutting after a comma four characters in leaves a hole on the right.

    The window is 10 % of the line, so punctuation is a tie-breaker for the last few
    characters — not a target. Searching the whole line produced ragged pages.
    """
    lines = wrap("甲，乙丙丁戊己庚", max_width=_em_width(6, 10), measure=_EM)
    assert lines[0] == "甲，乙丙丁戊"


def test_a_latin_word_is_not_cut_in_half() -> None:
    """「《BI / BBIDIBA》」 reads as a typo; product names are what a reader notices most."""
    lines = wrap("他说的是《BIBBIDIBA》这个作品", max_width=_em_width(11, 10), measure=_EM)
    assert lines[0] == "他说的是"
    assert lines[1].startswith("《BIBBIDIBA》"), "the name must stay whole, bracket included"


def test_a_latin_run_longer_than_the_line_still_breaks_by_width() -> None:
    """Otherwise a long URL would move the break back to 0 and loop forever."""
    lines = wrap("BIBBIDIBAANDMORE", max_width=_em_width(6, 10), measure=_EM)
    assert lines[0] == "BIBBID"
    assert "".join(lines) == "BIBBIDIBAANDMORE"


def test_a_line_does_not_end_with_an_opening_mark() -> None:
    """「比如说《」 at the end of a line, with the name starting the next one, is a typo.

    The real page that prompted this had 《 on one line and BIBBIDIBA》 on the next.
    """
    lines = wrap("比如说什么什么《BIBBIDIBA》", max_width=_em_width(14, 10), measure=_EM)
    assert all(not line.endswith(tuple(NO_LINE_END)) for line in lines)


def test_the_first_line_is_never_merged_anywhere() -> None:
    """A page whose body *starts* with 「，」 keeps it: there is nothing before it."""
    lines = wrap("，开头" + "甲" * 10, max_width=_em_width(6, 10), measure=_EM)
    assert lines[0].startswith("，")


# --- layout -------------------------------------------------------------


def test_a_page_that_fits_uses_the_maximum_size() -> None:
    plan = layout(
        title="标题", body="正文", width=600, height=400, measure=measure, max_body_px=40
    )
    assert plan.body_px == 40


def test_a_full_page_shrinks_instead_of_overflowing() -> None:
    body = "字" * 200
    plan = layout(title="", body=body, width=600, height=400, measure=measure, max_body_px=48)
    assert plan.height_px <= 400
    assert plan.body_px < 48


def test_shrinking_stops_at_the_floor() -> None:
    """Below the floor the page is unreadable; overflowing slightly is the honest failure."""
    plan = layout(
        title="", body="字" * 5000, width=200, height=100, measure=measure, max_body_px=56
    )
    assert plan.body_px == MIN_BODY_PX


def test_the_title_is_larger_than_the_body() -> None:
    plan = layout(title="标题", body="正文", width=800, height=600, measure=measure)
    assert plan.title_px > plan.body_px


def test_the_title_is_capped() -> None:
    plan = layout(title="标题", body="正文", width=4000, height=3000, measure=measure)
    assert plan.title_px <= MAX_TITLE_PX


def test_a_long_title_is_brought_down_to_two_lines() -> None:
    title = "很长很长的标题" * 2
    plan = layout(title=title, body="", width=400, height=400, measure=measure)
    assert len(plan.title_lines) <= 2
    assert plan.title_px >= MIN_TITLE_PX


def test_a_title_too_long_for_two_lines_stops_at_the_floor() -> None:
    """The floor wins over the two-line rule, and that is deliberate.

    Shrinking further would fit the title but make it unreadable, and a page whose title
    cannot be read has no reason to exist. The layout is self-consistent either way: the
    body's shrink loop counts the title block, so an over-long title costs body lines
    rather than pushing text off the page without anyone noticing.
    """
    plan = layout(title="很长很长的标题" * 8, body="正文", width=400, height=600, measure=measure)
    assert plan.title_px == MIN_TITLE_PX
    assert len(plan.title_lines) > 2


def test_the_body_starts_below_the_title() -> None:
    plan = layout(title="标题", body="正文", width=800, height=600, measure=measure)
    assert plan.body_top_px > 0


def test_no_title_means_the_body_starts_at_the_top() -> None:
    plan = layout(title="", body="正文", width=800, height=600, measure=measure)
    assert plan.title_lines == ()
    assert plan.body_top_px == 0


def test_layout_is_deterministic() -> None:
    """Same input, same output — the product promise is「同项目 → 同视频」."""
    kwargs = {"title": "标题" * 5, "body": "正文" * 40, "width": 700, "height": 400}
    first = layout(measure=measure, **kwargs)
    second = layout(measure=measure, **kwargs)
    assert first == second


def test_height_accounts_for_every_body_line() -> None:
    plan = layout(title="", body="字" * 60, width=300, height=600, measure=measure)
    expected = (
        plan.body_top_px
        + max(0, len(plan.body_lines) - 1) * plan.leading_px
        + plan.body_px
    )
    assert plan.height_px == expected


@pytest.mark.parametrize("width", [200, 400, 800, 1600])
def test_a_fitting_plan_never_exceeds_the_box(width: int) -> None:
    """The invariant the auto-shrink exists to guarantee."""
    plan = layout(
        title="一个标题", body="正文" * 30, width=width, height=400, measure=measure
    )
    if plan.body_px > MIN_BODY_PX:
        assert plan.height_px <= 400
