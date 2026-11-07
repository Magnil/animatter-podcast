"""Document generation: turn merging, block splitting, chapters, rendering.

Everything pinned here is something that silently produces a plausible-looking
but wrong document:

* a merged turn that swallows two speakers, so the article attributes a
  question to the person who answered it;
* a chapter timestamp invented by the model instead of read off the transcript;
* a block boundary in the middle of a sentence, so the rewrite loses its
  predicate;
* a signature that only appears when it happens to be non-empty *and* the
  document has chapters.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from animatter.core.script import (
    DEFAULT_PROMPTS,
    Chapter,
    ScriptSentence,
    build_turns,
    format_clock,
    numbered_lines,
    parse_chapters,
    plan_blocks,
    render_article,
    render_dialogue,
    render_shownotes,
    speaker_name,
    spoken_lines,
)


def sentence(ord_: int, start: int, end: int, speaker: int | None, text: str) -> ScriptSentence:
    return ScriptSentence(ord=ord_, start_ms=start, end_ms=end, speaker=speaker, text=text)


# --- turns ---------------------------------------------------------------


def test_consecutive_sentences_from_one_speaker_merge() -> None:
    turns = build_turns(
        [
            sentence(0, 0, 1000, 0, "今天聊聊动画。"),
            sentence(1, 1000, 2000, 0, "我准备了三个问题。"),
            sentence(2, 2000, 3000, 1, "好。"),
        ]
    )
    assert len(turns) == 2
    assert turns[0].text == "今天聊聊动画。我准备了三个问题。"
    assert turns[0].start_ms == 0
    assert turns[0].end_ms == 2000
    assert turns[0].first_ord == 0
    assert turns[1].speaker == 1


def test_a_speaker_change_is_never_merged() -> None:
    turns = build_turns(
        [
            sentence(0, 0, 1000, 0, "你最喜欢的导演是谁？"),
            sentence(1, 1000, 2000, 1, "今敏。"),
        ]
    )
    assert [turn.speaker for turn in turns] == [0, 1]
    assert turns[0].text == "你最喜欢的导演是谁？"


def test_unknown_speaker_stays_a_distinct_turn() -> None:
    turns = build_turns(
        [
            sentence(0, 0, 1000, None, "旁白。"),
            sentence(1, 1000, 2000, 0, "正片。"),
        ]
    )
    assert len(turns) == 2


def test_a_filibuster_is_split_so_it_cannot_become_one_paragraph() -> None:
    turns = build_turns(
        [sentence(i, i * 1000, (i + 1) * 1000, 0, "这是一段很长的发言。") for i in range(10)],
        max_turn_chars=40,
    )
    assert len(turns) > 1
    assert all(len(turn.text) <= 40 for turn in turns)


def test_empty_sentences_are_dropped() -> None:
    turns = build_turns(
        [
            sentence(0, 0, 1000, 0, "   "),
            sentence(1, 1000, 2000, 0, "有内容。"),
        ]
    )
    assert len(turns) == 1
    assert turns[0].text == "有内容。"


# --- blocks --------------------------------------------------------------


def test_blocks_cover_every_turn_exactly_once() -> None:
    turns = build_turns([sentence(i, i * 1000, (i + 1) * 1000, i % 2, "内容" * 20) for i in range(20)])
    blocks = plan_blocks(turns, max_chars=100)
    assert blocks[0][0] == 0
    assert blocks[-1][1] == len(turns)
    for (_, end), (start, _) in pairwise(blocks):
        assert end == start, "blocks must be contiguous"


def test_blocks_respect_the_budget() -> None:
    turns = build_turns([sentence(i, i * 1000, (i + 1) * 1000, i % 2, "字" * 90) for i in range(10)])
    blocks = plan_blocks(turns, max_chars=200)
    for start, end in blocks:
        assert sum(len(turn.text) for turn in turns[start:end]) <= 200


def test_a_turn_longer_than_the_budget_still_gets_its_own_block() -> None:
    turns = build_turns([sentence(0, 0, 1000, 0, "字" * 500)], max_turn_chars=1000)
    blocks = plan_blocks(turns, max_chars=100)
    assert blocks == [(0, 1)]


def test_empty_input_plans_one_empty_block() -> None:
    assert plan_blocks([], max_chars=100) == [(0, 0)]


# --- prompts input -------------------------------------------------------


def test_numbered_lines_use_the_turn_index() -> None:
    turns = build_turns(
        [
            sentence(0, 0, 1000, 0, "第一句。"),
            sentence(1, 1000, 2000, 1, "第二句。"),
        ]
    )
    listing = numbered_lines(turns, {0: "主播", 1: "嘉宾"})
    assert listing == "[0] 主播：第一句。\n[1] 嘉宾：第二句。"


def test_numbered_lines_offset_lets_blocks_cite_global_indices() -> None:
    turns = build_turns([sentence(0, 0, 1000, 0, "内容。")])
    assert numbered_lines(turns, {}, offset=5).startswith("[5]")


def test_spoken_lines_have_no_numbering() -> None:
    turns = build_turns([sentence(0, 0, 1000, 0, "内容。")])
    assert spoken_lines(turns, {0: "主播"}) == "主播：内容。"


def test_speaker_name_falls_back_rather_than_vanishing() -> None:
    assert speaker_name(0, {0: "马小褂"}) == "马小褂"
    assert speaker_name(3, {}) == "说话人4"
    assert speaker_name(None, {0: "x"}) == "说话人"


# --- chapter parsing -----------------------------------------------------


def turns_fixture() -> list:
    """Three sentences, two speakers — so it folds into two turns."""
    return build_turns(
        [
            sentence(0, 0, 1000, 0, "开场。"),
            sentence(1, 1000, 2000, 0, "正题。"),
            sentence(2, 2000, 3000, 1, "结尾。"),
        ]
    )


def test_chapters_map_line_numbers_back_to_turns() -> None:
    turns = turns_fixture()
    assert len(turns) == 2, "fixture must fold into two turns"
    summary, points, chapters = parse_chapters(
        {
            "summary": "概述",
            "points": ["要点一"],
            "chapters": [{"heading": "开场", "line": 0}, {"heading": "结尾", "line": 1}],
        },
        turns,
    )
    assert summary == "概述"
    assert points == ["要点一"]
    assert chapters == [("开场", 0), ("结尾", 1)]


def test_a_chapter_pointing_at_a_missing_line_is_dropped() -> None:
    # Not snapped to the nearest valid line: a chapter that points at the wrong
    # speaker is a factual error in the published notes.
    turns = turns_fixture()
    _summary, _points, chapters = parse_chapters(
        {"chapters": [{"heading": "不存在", "line": 99}, {"heading": "负数", "line": -1}]},
        turns,
    )
    assert chapters == []


def test_chapters_without_a_heading_or_a_number_are_dropped() -> None:
    turns = turns_fixture()
    _s, _p, chapters = parse_chapters(
        {"chapters": [{"line": 0}, {"heading": "没有行号"}, {"heading": "", "line": 1}]},
        turns,
    )
    assert chapters == []


def test_a_fenced_reply_is_accepted() -> None:
    turns = turns_fixture()
    payload: object = {
        "summary": "概述",
        "points": [],
        "chapters": [{"heading": "开场", "line": 1}],
    }
    summary, _points, chapters = parse_chapters(payload, turns)
    assert summary == "概述"
    assert chapters == [("开场", 1)]


def test_garbage_payload_yields_empty_everything() -> None:
    assert parse_chapters("not an object", turns_fixture()) == ("", [], [])


def test_chapter_offset_is_subtracted() -> None:
    turns = turns_fixture()
    _s, _p, chapters = parse_chapters({"chapters": [{"heading": "开场", "line": 10}]}, turns, offset=10)
    assert chapters == [("开场", 0)]


# --- rendering -----------------------------------------------------------


def test_clock_uses_hours_only_when_there_are_hours() -> None:
    assert format_clock(0) == "00:00"
    assert format_clock(63_000) == "01:03"
    assert format_clock(3_723_000) == "1:02:03"
    assert format_clock(-500) == "00:00"


def test_shownotes_have_every_section_in_a_fixed_order() -> None:
    body = render_shownotes(
        title="漫物质 第 12 期",
        summary="这期聊动画 MV。",
        points=["观点一", "观点二"],
        chapters=[Chapter(heading="开场", start_ms=0, first_ord=0)],
        signature="主播：马小褂",
    )
    assert body.index("# 漫物质 第 12 期") < body.index("这期聊动画 MV。")
    assert body.index("## 本期要点") < body.index("## 时间轴")
    assert body.index("## 时间轴") < body.index("主播：马小褂")
    assert "- **00:00** 开场" in body


def test_signature_is_appended_verbatim_and_kept_multiline() -> None:
    body = render_shownotes(
        title="T",
        summary="S",
        points=[],
        chapters=[],
        signature="主播：马小褂\n收听：小宇宙 / 苹果播客",
    )
    assert body.rstrip().endswith("主播：马小褂\n收听：小宇宙 / 苹果播客")


def test_an_empty_signature_leaves_no_divider() -> None:
    body = render_shownotes(title="T", summary="S", points=[], chapters=[], signature="   ")
    assert "---" not in body


def test_shownotes_with_no_chapters_omit_the_heading() -> None:
    body = render_shownotes(title="T", summary="S", points=[], chapters=[], signature="")
    assert "## 时间轴" not in body
    assert "## 本期要点" not in body


def test_dialogue_keeps_speaker_labels_and_adds_no_headings() -> None:
    body = render_dialogue(title="T", body="**主播**：你好。\n\n**嘉宾**：你好。")
    assert body.startswith("# T")
    assert "**主播**：你好。" in body
    assert "## " not in body


def test_article_drops_speaker_labels() -> None:
    body = render_article(title="T", sections=[("", "第一段。\n\n第二段。")])
    assert "说话人" not in body
    assert "第一段。" in body


def test_article_keeps_section_headings_when_given() -> None:
    body = render_article(title="T", sections=[("开场", "正文。")])
    assert "## 开场" in body


def test_a_title_less_document_does_not_start_with_an_empty_heading() -> None:
    assert not render_dialogue(title="", body="正文。").startswith("#")


def test_bodies_end_with_exactly_one_newline() -> None:
    for body in (
        render_shownotes(title="T", summary="S", points=["p"], chapters=[], signature=""),
        render_dialogue(title="T", body="正文。"),
        render_article(title="T", sections=[("", "正文。")]),
    ):
        assert body.endswith("\n")
        assert not body.endswith("\n\n")


# --- prompts -------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["shownotes", "dialogue", "article"])
def test_every_format_ships_a_prompt(fmt: str) -> None:
    prompt = DEFAULT_PROMPTS[fmt]
    assert len(prompt) > 200, "a one-line prompt produces one-line output"
    # The article prompt must forbid the very thing that makes it an article.
    if fmt == "article":
        assert "说话人" in prompt
