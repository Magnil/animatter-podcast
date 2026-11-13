"""Topic planning: the model's outline, and the repair that makes it renderable (§19.15).

Three groups, and each one exists because round 100 replaced a deterministic page cutter with
a model-written outline, so the ways the new path can silently produce a bad deck are new:

* **The parse.** A model reply is data from outside the process. A page pointing at a line
  that does not exist, a bullet 60 characters long, four points where the slide fits three —
  each of those produces a page that *looks* generated and is wrong on screen.
* **The repair.** Page starts, minimum duration, the page cap and the chapter headings are all
  decisions taken after the model speaks, and every one of them is a place where "merge" can
  turn into "drop".
* **The fallback.** It still exists, and the one thing that must not happen is a fallback deck
  arriving without saying so.
"""

from __future__ import annotations

from animatter.core.deck import (
    CHAPTER_SNAP_MS,
    MAX_PAGES,
    MAX_POINT_CHARS,
    MAX_POINTS,
    MIN_TOPIC_MS,
    POINT_MARKER,
    PagePlan,
    clean_points,
    parse_outline,
    plan_pages,
    settle_pages,
)
from animatter.core.script import Chapter, ScriptSentence, Turn


def sentence(
    ord_: int, start: int, end: int, text: str = "内容。", speaker: int | None = 0
) -> ScriptSentence:
    return ScriptSentence(ord=ord_, start_ms=start, end_ms=end, speaker=speaker, text=text)


def turn(index: int, start: int, duration: int = 60_000, text: str = "内容。") -> Turn:
    return Turn(speaker=0, start_ms=start, end_ms=start + duration, first_ord=index, text=text)


def episode(count: int, *, duration: int = 60_000) -> list[Turn]:
    return [turn(index, index * duration, duration) for index in range(count)]


def sentences_for(turns: list[Turn]) -> list[ScriptSentence]:
    """One sentence per turn, which is all ``settle_pages`` needs to read ords and context."""
    return [
        sentence(item.first_ord, item.start_ms, item.end_ms, item.text) for item in turns
    ]


# --- parsing a reply ----------------------------------------------------


def test_a_plain_outline_becomes_pages() -> None:
    turns = episode(3)
    pages, warnings = parse_outline(
        {
            "pages": [
                {"line": 0, "title": "开场", "points": ["为什么做这期", "这期聊什么"]},
                {"line": 1, "title": "做片六年", "points": ["第一年只有三个人", "第六年才有预算"]},
            ]
        },
        turns,
    )
    assert warnings == []
    assert [page.title for page in pages] == ["开场", "做片六年"]
    assert pages[0].points == ("为什么做这期", "这期聊什么")
    assert [page.start_ms for page in pages] == [0, 60_000]
    assert pages[0].source_ord == 0 and pages[1].source_ord == 1


def test_a_page_body_is_the_points_with_a_marker() -> None:
    """The renderer draws body lines verbatim, so the bullet has to be in the string."""
    pages, _warnings = parse_outline(
        {"pages": [{"line": 0, "title": "T", "points": ["甲", "乙"]}]}, episode(1)
    )
    assert pages[0].body == f"{POINT_MARKER}甲\n{POINT_MARKER}乙"


def test_a_line_number_that_does_not_exist_is_dropped_not_snapped() -> None:
    """Snapping would attach a page to the wrong topic with nothing on screen saying so."""
    pages, warnings = parse_outline(
        {"pages": [{"line": 0, "title": "好的"}, {"line": 99, "title": "越界"}]}, episode(3)
    )
    assert [page.title for page in pages] == ["好的"]
    assert any("行号" in warning for warning in warnings)


def test_a_page_without_a_line_number_is_dropped() -> None:
    pages, warnings = parse_outline({"pages": [{"title": "没有行号"}]}, episode(2))
    assert pages == []
    assert any("行号" in warning for warning in warnings)


def test_a_page_with_neither_title_nor_points_is_dropped() -> None:
    pages, warnings = parse_outline({"pages": [{"line": 0, "title": "  "}]}, episode(1))
    assert pages == []
    assert any("没有标题也没有要点" in warning for warning in warnings)


def test_a_title_only_page_becomes_a_section_page() -> None:
    """A chapter break is a legitimate slide: a big title and nothing under it."""
    pages, _warnings = parse_outline({"pages": [{"line": 0, "title": "第二章"}]}, episode(1))
    assert pages[0].layout == "title"
    assert pages[0].points == ()


def test_a_titleless_page_takes_its_first_point_as_the_title() -> None:
    pages, _warnings = parse_outline(
        {"pages": [{"line": 0, "points": ["他先说结论", "再讲原因"]}]}, episode(1)
    )
    assert pages[0].title == "他先说结论"
    assert pages[0].points == ("他先说结论", "再讲原因")


def test_points_are_capped_and_deduplicated() -> None:
    raw = ["甲", "乙", "甲", "丙", "丁", "戊"]
    assert clean_points(raw) == ("甲", "乙", "丙", "丁")
    assert len(clean_points(raw)) == MAX_POINTS


def test_a_leading_bullet_or_number_is_stripped() -> None:
    assert clean_points(["・第一条", "- 第二条", "1. 第三条", "2、第四条"]) == (
        "第一条",
        "第二条",
        "第三条",
        "第四条",
    )


def test_a_long_point_is_cut_at_the_cap() -> None:
    cleaned = clean_points(["字" * 80])
    assert len(cleaned[0]) == MAX_POINT_CHARS


def test_points_may_arrive_as_one_newline_separated_string() -> None:
    """Models sometimes answer with a string where the schema asked for a list."""
    assert clean_points("甲\n乙；丙") == ("甲", "乙", "丙")


def test_points_may_arrive_as_objects() -> None:
    assert clean_points([{"text": "甲"}, {"point": "乙"}]) == ("甲", "乙")


def test_a_reply_with_no_page_list_is_empty_not_an_error() -> None:
    pages, warnings = parse_outline({"summary": "答非所问"}, episode(2))
    assert pages == []
    assert warnings == []


def test_a_long_title_is_truncated_and_reported() -> None:
    pages, warnings = parse_outline(
        {"pages": [{"line": 0, "title": "字" * 60, "points": ["甲"]}]}, episode(1)
    )
    assert len(pages[0].title) == 26
    assert any("标题" in warning for warning in warnings)


def test_a_nested_point_list_is_flattened_not_dropped() -> None:
    """Measured on a real reply: the model drifted to ``["甲", ["乙"], ["丙"]]`` mid-answer.

    The old parser saw three entries, kept one string and skipped two lists, so 16 of 23 pages
    came back with a single bullet. Flattening turns a formatting quirk back into four points.
    """
    assert clean_points(["甲", ["乙"], ["丙", ["丁"]]]) == ("甲", "乙", "丙", "丁")


def test_a_page_with_one_point_is_reported() -> None:
    """A single bullet under a title reads as a bug, and is one; say so rather than pad it."""
    pages, warnings = parse_outline(
        {"pages": [{"line": 0, "title": "只有一条", "points": ["唯一"]}]}, episode(2)
    )
    assert len(pages) == 1
    assert any("只写出 1 条要点" in warning for warning in warnings)


# --- repairing the plan --------------------------------------------------


def test_pages_are_sorted_and_the_first_one_starts_at_zero() -> None:
    turns = episode(4)
    pages = [
        PagePlan(source_ord=2, start_ms=120_000, last_ord=2, title="丙", points=("甲",)),
        PagePlan(source_ord=0, start_ms=0, last_ord=0, title="甲", points=("甲",)),
    ]
    settled, warnings = settle_pages(pages, sentences=sentences_for(turns), total_ms=240_000)
    assert [page.title for page in settled] == ["甲", "丙"]
    assert warnings == []


def test_a_deck_that_starts_late_is_pulled_back_to_the_first_sentence() -> None:
    """Otherwise the box is empty for the first minutes of the film."""
    turns = episode(4)
    pages = [PagePlan(source_ord=2, start_ms=120_000, last_ord=2, title="丙", points=("甲",))]
    settled, warnings = settle_pages(pages, sentences=sentences_for(turns), total_ms=240_000)
    assert settled[0].start_ms == 0
    assert settled[0].source_ord == 0
    assert any("第一页从" in warning for warning in warnings)


def test_two_pages_at_the_same_instant_collapse_to_one() -> None:
    turns = episode(4)
    pages = [
        PagePlan(source_ord=0, start_ms=0, last_ord=0, title="甲", points=("甲",)),
        PagePlan(source_ord=1, start_ms=0, last_ord=1, title="乙", points=("乙",)),
        PagePlan(source_ord=3, start_ms=180_000, last_ord=3, title="丁", points=("丁",)),
    ]
    settled, warnings = settle_pages(pages, sentences=sentences_for(turns), total_ms=240_000)
    assert [page.title for page in settled] == ["甲", "丁"]
    assert any("时间点重合" in warning for warning in warnings)


def test_a_page_shorter_than_the_floor_is_merged_forward_with_its_points() -> None:
    """A ten-second page is a flicker, and its points must survive the merge."""
    turns = episode(4, duration=60_000)
    pages = [
        PagePlan(source_ord=0, start_ms=0, last_ord=0, title="甲", points=("甲",)),
        PagePlan(source_ord=1, start_ms=60_000, last_ord=1, title="乙", points=("乙",)),
        PagePlan(
            source_ord=2,
            start_ms=60_000 + MIN_TOPIC_MS // 2,
            last_ord=2,
            title="短",
            points=("丙",),
        ),
        PagePlan(source_ord=3, start_ms=200_000, last_ord=3, title="丁", points=("丁",)),
    ]
    settled, warnings = settle_pages(pages, sentences=sentences_for(turns), total_ms=260_000)
    assert len(settled) == 3, "the short page must fold into a neighbour"
    carrier = next(page for page in settled if "丙" in page.points)
    assert carrier.start_ms == 60_000, "the survivor keeps the earlier of the two starts"
    assert {"乙", "丙"} <= set(carrier.points), "both pages' points must survive"
    assert any("太短" in warning for warning in warnings)


def test_the_page_cap_merges_the_shortest_neighbour_rather_than_dropping_the_tail() -> None:
    """Truncating would silently lose the end of the conversation; merging keeps coverage."""
    turns = episode(MAX_PAGES + 10, duration=60_000)
    pages = [
        PagePlan(
            source_ord=item.first_ord,
            start_ms=item.start_ms,
            last_ord=item.first_ord,
            title=f"第{index}页",
            points=(f"要点{index}",),
        )
        for index, item in enumerate(turns)
    ]
    settled, warnings = settle_pages(
        pages, sentences=sentences_for(turns), total_ms=turns[-1].end_ms
    )
    assert len(settled) == MAX_PAGES
    assert settled[-1].title == f"第{len(turns) - 1}页", "the last page must still be the last topic"
    assert any("页数超过" in warning for warning in warnings)


def test_a_chapter_close_to_a_page_takes_over_its_title() -> None:
    turns = episode(3)
    pages = [
        PagePlan(source_ord=item.first_ord, start_ms=item.start_ms, last_ord=item.first_ord,
                 title=f"模型标题{index}", points=(f"要点{index}",))
        for index, item in enumerate(turns)
    ]
    settled, _warnings = settle_pages(
        pages,
        sentences=sentences_for(turns),
        total_ms=180_000,
        chapters=[Chapter(heading="第二章 长片与短片", start_ms=60_000, first_ord=1)],
    )
    assert settled[1].title == "第二章 长片与短片"
    assert settled[0].title == "模型标题0"


def test_a_chapter_with_no_page_near_it_becomes_a_section_page() -> None:
    turns = episode(3)
    pages = [
        PagePlan(source_ord=item.first_ord, start_ms=item.start_ms, last_ord=item.first_ord,
                 title=f"模型标题{index}", points=(f"要点{index}",))
        for index, item in enumerate(turns)
    ]
    settled, _warnings = settle_pages(
        pages,
        sentences=sentences_for(turns),
        total_ms=180_000,
        chapters=[
            Chapter(heading="插进来的章节", start_ms=90_000, first_ord=1),
        ],
    )
    inserted = [page for page in settled if page.title == "插进来的章节"]
    assert len(inserted) == 1
    assert inserted[0].layout == "title"
    assert inserted[0].start_ms == 90_000
    assert [page.start_ms for page in settled] == sorted(page.start_ms for page in settled)


def test_a_chapter_just_outside_the_snap_distance_gets_its_own_page() -> None:
    """The snap window is the whole rule; being one millisecond outside it must not retitle."""
    turns = episode(3)
    pages = [
        PagePlan(source_ord=item.first_ord, start_ms=item.start_ms, last_ord=item.first_ord,
                 title=f"模型标题{index}", points=(f"要点{index}",))
        for index, item in enumerate(turns)
    ]
    far = 60_000 + CHAPTER_SNAP_MS + 1
    settled, _warnings = settle_pages(
        pages,
        sentences=sentences_for(turns),
        total_ms=180_000,
        chapters=[Chapter(heading="差一毫秒", start_ms=far, first_ord=1)],
    )
    assert [page.title for page in settled].count("差一毫秒") == 1
    assert settled[0].title == "模型标题0"


def test_every_page_gets_its_context_from_the_sentence_slice() -> None:
    turns = episode(3)
    turns = [
        Turn(speaker=0, start_ms=item.start_ms, end_ms=item.end_ms, first_ord=item.first_ord,
             text=f"第{index}段的原文。")
        for index, item in enumerate(turns)
    ]
    pages = [
        PagePlan(source_ord=item.first_ord, start_ms=item.start_ms, last_ord=item.first_ord,
                 title=f"第{index}页", points=(f"要点{index}",))
        for index, item in enumerate(turns)
    ]
    settled, _warnings = settle_pages(pages, sentences=sentences_for(turns), total_ms=180_000)
    assert "第0段的原文" in settled[0].context
    assert "第1段的原文" in settled[1].context
    assert settled[0].last_ord == 0
    assert settled[-1].last_ord == 2


def test_an_empty_transcript_settles_to_nothing() -> None:
    settled, _warnings = settle_pages(
        [PagePlan(source_ord=0, start_ms=0, last_ord=0, title="空")], sentences=[], total_ms=0
    )
    assert settled == []


# --- the fallback is still the fallback ---------------------------------


def test_the_fallback_still_cuts_at_pauses() -> None:
    """Round 100 did not delete the deterministic planner; it demoted it."""
    pages = plan_pages([sentence(0, 0, 5_000, "第一句。"), sentence(1, 5_000, 10_000, "第二句。")])
    assert len(pages) == 1
    assert pages[0].text == "第一句。第二句。"
    assert pages[0].points == (), "the fallback has no points: its body is the transcript"


def test_fallback_pages_carry_no_points_so_the_body_is_the_transcript() -> None:
    pages = plan_pages([sentence(0, 0, 30_000, "一二三四五。")])
    assert pages[0].body == "一二三四五。"
    assert POINT_MARKER not in pages[0].body
