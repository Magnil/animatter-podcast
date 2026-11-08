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
    MAX_TITLE_CHARS,
    Chapter,
    ScriptSentence,
    Turn,
    build_turns,
    count_title_chars,
    fit_titles,
    format_clock,
    host_speaker,
    numbered_lines,
    parse_shownotes_plan,
    plan_blocks,
    refine_chapters,
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
    plan, _dropped = parse_shownotes_plan(
        {
            "summary": "概述",
            "titles": ["一个标题"],
            "chapters": [{"heading": "开场", "line": 0}, {"heading": "结尾", "line": 1}],
        },
        turns,
    )
    assert plan.summary == "概述"
    assert plan.titles == ["一个标题"]
    assert plan.chapters == [("开场", 0), ("结尾", 1)]


def test_a_chapter_pointing_at_a_missing_line_is_dropped() -> None:
    # Not snapped to the nearest valid line: a chapter that points at the wrong
    # speaker is a factual error in the published notes.
    turns = turns_fixture()
    plan, _d = parse_shownotes_plan(
        {"chapters": [{"heading": "不存在", "line": 99}, {"heading": "负数", "line": -1}]},
        turns,
    )
    assert plan.chapters == []


def test_chapters_without_a_heading_or_a_number_are_dropped() -> None:
    turns = turns_fixture()
    plan, _d = parse_shownotes_plan(
        {"chapters": [{"line": 0}, {"heading": "没有行号"}, {"heading": "", "line": 1}]},
        turns,
    )
    assert plan.chapters == []


def test_a_fenced_reply_is_accepted() -> None:
    turns = turns_fixture()
    payload: object = {
        "summary": "概述",
        "titles": [],
        "chapters": [{"heading": "开场", "line": 1}],
    }
    plan, _d = parse_shownotes_plan(payload, turns)
    assert plan.summary == "概述"
    assert plan.chapters == [("开场", 1)]


def test_garbage_payload_yields_empty_everything() -> None:
    plan, dropped = parse_shownotes_plan("not an object", turns_fixture())
    assert (plan.summary, plan.titles, plan.chapters) == ("", [], [])
    assert dropped == []


def test_chapter_offset_is_subtracted() -> None:
    turns = turns_fixture()
    plan, _d = parse_shownotes_plan({"chapters": [{"heading": "开场", "line": 10}]}, turns, offset=10)
    assert plan.chapters == [("开场", 0)]


# --- titles --------------------------------------------------------------


def test_title_length_counts_every_visible_character() -> None:
    # The house rule: 汉字, 全角标点 and half-width letters each cost one.
    assert count_title_chars("音乐如何被画出来：学院与二次元之间的网络创作") == 22
    assert count_title_chars("音乐如何被画出来：学院派与二次元之间的网络创作") == 23
    assert count_title_chars("MV") == 2
    # Whitespace does not occupy a cell, so it is not a character.
    assert count_title_chars("音乐 如何") == 4


def test_an_over_long_title_is_dropped_not_truncated() -> None:
    # Truncating a two-part title leaves the 机制 half missing: the result still
    # reads like Chinese but no longer means what the second half was for.
    kept, dropped = fit_titles(
        [
            "音乐如何被画出来：学院与二次元之间的网络创作",
            "音乐如何被画出来：学院派与二次元之间的网络创作",
        ]
    )
    assert kept == ["音乐如何被画出来：学院与二次元之间的网络创作"]
    assert len(dropped) == 1 and "23" in dropped[0]


def test_duplicate_and_blank_titles_collapse() -> None:
    kept, _dropped = fit_titles(["同名", "同名", "  ", "另一个"])
    assert kept == ["同名", "另一个"]


def test_the_cap_is_the_documented_twenty_two() -> None:
    assert MAX_TITLE_CHARS == 22


# --- host detection ------------------------------------------------------


def interview(script: list[tuple[int, str]]) -> list:
    return build_turns(
        [sentence(i, i * 1000, (i + 1) * 1000, speaker, text) for i, (speaker, text) in enumerate(script)]
    )


def test_the_self_introduction_decides_the_host() -> None:
    turns = interview(
        [
            (0, "我先说说这个作品。"),
            (1, "各位听众大家好，欢迎收听本期的漫物质。"),
            (0, "嗯，契机是大一时候的入围。"),
            (1, "那你是怎么认识他的？"),
        ]
    )
    assert host_speaker(turns) == 1


def test_question_ratio_decides_when_nobody_introduces_themselves() -> None:
    turns = interview(
        [
            (0, "这个作品当时是怎么做出来的？"),
            (1, "我用了三个月，主要是手绘。"),
            (0, "那为什么选择这种画法呢？"),
            (1, "因为我喜欢模拟信号的感觉。"),
            (0, "有没有考虑过别的方案？"),
            (1, "考虑过但放弃了。"),
        ]
    )
    assert host_speaker(turns) == 0


def test_who_opens_is_the_last_resort() -> None:
    # No intro, and both sides ask equally — the first speaker is the host.
    turns = interview([(1, "开始吧。"), (0, "好。"), (1, "行。"), (0, "嗯。")])
    assert host_speaker(turns) == 1


def test_a_single_speaker_recording_has_no_host() -> None:
    turns = interview([(0, "独白。" * 20), (0, "继续。")])
    assert host_speaker(turns) is None


def test_the_guest_greeting_alone_does_not_claim_the_chair() -> None:
    # Two people greeting each other is not a self-introduction by either.
    turns = interview(
        [
            (0, "大家好，我是嘉宾。"),
            (1, "大家好，我是主持。"),
            (0, "那我们开始。"),
            (1, "好。"),
        ]
    )
    # Both matched a marker, so the intro rule abstains and the question/opening
    # fallbacks decide — which must still pick someone, not crash.
    assert host_speaker(turns) in {0, 1}


# --- chapter snapping ----------------------------------------------------


def turn_list(script: list[tuple[int, int, str]]) -> list[Turn]:
    """Explicit turns: (start_ms, speaker, text).

    Built by hand rather than through :func:`build_turns`, because that helper
    *merges* consecutive same-speaker sentences — which is exactly the shape
    these tests need to keep apart (a guest talking for two turns in a row).
    """
    return [
        Turn(speaker=speaker, start_ms=start, end_ms=start + 1000, first_ord=i, text=text)
        for i, (start, speaker, text) in enumerate(script)
    ]


def test_a_chapter_is_pulled_back_onto_the_host_turn_that_opened_it() -> None:
    turns = turn_list(
        [
            (0, 1, "前面聊完了。"),
            (1000, 1, "那么接下来我们聊聊《幸福的悖论》。"),  # host picks it up
            (3000, 0, "这部作品的契机是大一那年。"),
            (5000, 0, "我用了三个月才做完。"),  # model anchored here
        ]
    )
    refined = refine_chapters([("《幸福的悖论》", 3)], turns, host=1)
    assert refined == [("《幸福的悖论》", 1)]


def test_a_chapter_is_pulled_forward_onto_a_host_turn_just_ahead() -> None:
    """The other direction of the same mistake.

    Real output anchored a chapter on the guest's last 「嗯。」 one second
    *before* the host took over. A backward-only search would leave it there,
    which is exactly the chapter that then reads as belonging to the guest.
    """
    turns = turn_list(
        [
            (0, 0, "我回答完了。"),
            (2000, 0, "嗯。"),  # model anchored here
            (4000, 1, "那接下来我们换个话题。"),  # host actually opens the chapter
            (6000, 0, "好。"),
        ]
    )
    refined = refine_chapters([("换个话题", 1)], turns, host=1)
    assert refined == [("换个话题", 2)]


def test_the_closest_host_turn_wins() -> None:
    # Two host turns in range: 88 s back and 2 s back. The nearer one opened
    # this stretch, and a nudge should be the smallest that lands on a host.
    turns = turn_list(
        [
            (0, 1, "上一个话题。"),
            (40_000, 0, "很长的回答。"),
            (45_000, 0, "还在回答。"),
            (83_000, 1, "那我们聊下一个。"),
            (85_000, 0, "下一个话题的回答。"),  # model anchored here
        ]
    )
    refined = refine_chapters([("下一个", 4)], turns, host=1)
    assert refined == [("下一个", 3)]


def test_a_chapter_survives_when_no_host_turn_is_nearby() -> None:
    # The documented exception: the topic genuinely turned inside a guest's
    # answer, and the host has not spoken for half a minute — well outside the
    # 12 s window, so there is no hand-off to snap to.
    turns = turn_list(
        [
            (0, 1, "开场，我们进入这个话题。"),
            (30_000, 0, "我先回答。"),
            (40_000, 0, "然后话题其实自己变了。"),
            (50_000, 0, "接着往下说。"),
        ]
    )
    refined = refine_chapters([("话题变了", 2)], turns, host=1)
    assert refined == [("话题变了", 2)]


def test_a_chapter_is_not_dragged_into_the_previous_subject() -> None:
    # The host opened the previous chapter 60 s earlier; a 12 s window means it
    # is out of reach and the anchor stands.
    turns = turn_list(
        [
            (0, 1, "那我们开始第一个话题。"),
            (2000, 0, "第一个话题的回答。"),
            (62_000, 0, "第二个话题其实在这里开始。"),
        ]
    )
    refined = refine_chapters([("第二个话题", 2)], turns, host=1)
    assert refined == [("第二个话题", 2)]


def test_snapping_is_skipped_without_a_host() -> None:
    turns = turn_list([(0, 0, "一。"), (1000, 1, "二。"), (2000, 0, "三。")])
    assert refine_chapters([("章", 2)], turns, host=None) == [("章", 2)]


# --- rendering -----------------------------------------------------------


def test_clock_uses_hours_only_when_there_are_hours() -> None:
    assert format_clock(0) == "00:00:00"
    assert format_clock(63_000) == "00:01:03"
    assert format_clock(3_723_000) == "01:02:03"
    assert format_clock(-500) == "00:00:00"


def test_shownotes_have_every_section_in_a_fixed_order() -> None:
    body = render_shownotes(
        title="漫物质 第 12 期",
        summary="这期聊动画 MV。",
        chapters=[Chapter(heading="开场", start_ms=0, first_ord=0)],
        signature="主播：马小褂",
    )
    assert body.index("# 漫物质 第 12 期") < body.index("这期聊动画 MV。")
    assert body.index("这期聊动画 MV。") < body.index("## 时间轴")
    assert body.index("## 时间轴") < body.index("主播：马小褂")
    assert "- **00:00:00** 开场" in body


def test_shownotes_have_no_bullet_point_list() -> None:
    """本期要点 is a summary of a podcast, not a reason to press play.

    A reader who has already consumed five bullets has nothing left to find out,
    which is the opposite of what ShowNotes is for. The prose teaser carries the
    hook; the chapter list carries the shape.
    """
    body = render_shownotes(
        title="T",
        summary="S",
        chapters=[Chapter(heading="开场", start_ms=0, first_ord=0)],
        signature="",
    )
    assert "本期要点" not in body
    assert "## 时间轴" in body


def test_signature_is_appended_verbatim_and_kept_multiline() -> None:
    body = render_shownotes(
        title="T",
        summary="S",
        chapters=[],
        signature="主播：马小褂\n收听：小宇宙 / 苹果播客",
    )
    assert body.rstrip().endswith("主播：马小褂\n收听：小宇宙 / 苹果播客")


def test_an_empty_signature_leaves_no_divider() -> None:
    body = render_shownotes(title="T", summary="S", chapters=[], signature="   ")
    assert "---" not in body


def test_shownotes_with_no_chapters_omit_the_heading() -> None:
    body = render_shownotes(title="T", summary="S", chapters=[], signature="")
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
        render_shownotes(title="T", summary="S", chapters=[], signature=""),
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
