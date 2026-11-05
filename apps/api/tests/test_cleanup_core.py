"""Cleanup planning: what gets cut, and the rules that stop it cutting too much.

The load-bearing test here is :func:`test_short_word_between_two_silences_survives`
— Rescript shipped the bug where "remove silences" ate a 60 ms word sitting
between two pauses, and had to fix it in PR #115. Our EDL is harsher than theirs
(it drops keep-islands under 80 ms), so the same mistake would delete audio the
transcript still shows. The rule is asserted rather than trusted.
"""

from __future__ import annotations

import pytest

from animatter.core.cleanup import (
    CleanupItem,
    PlanRequest,
    TimeRange,
    build_plan,
    complement,
    find_disfluencies,
    find_filler_cuts,
    find_silence_cuts,
    subtract,
)

TOKENS: list[tuple[int, str, int, int]] = [
    (0, "今天", 0, 300),
    (1, "嗯", 300, 520),
    (2, "我们", 520, 900),
    (3, "聊聊", 900, 1300),
]
FILLERS = frozenset({"嗯", "呃", "额", "唔"})


# --- range arithmetic ---------------------------------------------------


def test_subtract_removes_covered_part() -> None:
    assert subtract([TimeRange(0, 1000)], [TimeRange(200, 400)]) == [
        TimeRange(0, 200),
        TimeRange(400, 1000),
    ]


def test_subtract_keeps_touching_ranges() -> None:
    """Half-open ranges: a cut ending at 500 does not touch [500, 600)."""
    assert subtract([TimeRange(500, 600)], [TimeRange(0, 500)]) == [TimeRange(500, 600)]


def test_subtract_handles_multiple_cuts() -> None:
    assert subtract([TimeRange(0, 1000)], [TimeRange(100, 200), TimeRange(500, 600)]) == [
        TimeRange(0, 100),
        TimeRange(200, 500),
        TimeRange(600, 1000),
    ]


def test_complement_of_nothing_is_everything() -> None:
    assert complement([], start_ms=0, end_ms=1000) == [TimeRange(0, 1000)]


# --- fillers ------------------------------------------------------------


def test_filler_cut_is_exactly_the_token() -> None:
    cuts = find_filler_cuts(TOKENS, fillers=FILLERS)
    assert len(cuts) == 1
    assert (cuts[0].start_ms, cuts[0].end_ms) == (300, 520)
    assert cuts[0].token_indices == (1,)


def test_filler_cut_does_not_swallow_the_pause_around_it() -> None:
    """Silence has its own switch; a filler cut must be the word, not the breath."""
    cuts = find_filler_cuts(TOKENS, fillers=FILLERS)
    assert cuts[0].duration_ms == 220


def test_deleted_filler_is_not_proposed_again() -> None:
    cuts = find_filler_cuts(TOKENS, fillers=FILLERS, deleted={1})
    assert cuts == []


def test_filler_cut_records_its_sentence() -> None:
    cuts = find_filler_cuts(TOKENS, fillers=FILLERS, sentence_of={1: 7})
    assert cuts[0].sentence_ord == 7


def test_zero_length_token_is_ignored() -> None:
    cuts = find_filler_cuts([(0, "嗯", 100, 100)], fillers=FILLERS)
    assert cuts == []


# --- silences -----------------------------------------------------------


def test_silence_between_words_is_cut_with_padding() -> None:
    tokens = [(0, "甲", 0, 500), (1, "乙", 1500, 2000)]
    cuts = find_silence_cuts(
        tokens,
        duration_ms=2000,
        silences=[TimeRange(500, 1500)],
        min_silence_ms=300,
        pad_ms=50,
    )
    assert len(cuts) == 1
    # 50ms held back on each side so neither word's tail is clipped.
    assert (cuts[0].start_ms, cuts[0].end_ms) == (550, 1450)


def test_silence_shorter_than_threshold_is_ignored() -> None:
    tokens = [(0, "甲", 0, 500), (1, "乙", 700, 1000)]
    cuts = find_silence_cuts(
        tokens, duration_ms=1000, silences=[TimeRange(500, 700)], min_silence_ms=300
    )
    assert cuts == []


def test_silence_at_media_start_is_cut_flush() -> None:
    """A file must not open with a stray fraction of a second."""
    tokens = [(0, "甲", 900, 1200)]
    cuts = find_silence_cuts(
        tokens, duration_ms=1200, silences=[TimeRange(0, 900)], min_silence_ms=300, pad_ms=50
    )
    assert (cuts[0].start_ms, cuts[0].end_ms) == (0, 850)


def test_silence_at_media_end_is_cut_flush() -> None:
    tokens = [(0, "甲", 0, 300)]
    cuts = find_silence_cuts(
        tokens, duration_ms=1200, silences=[TimeRange(300, 1200)], min_silence_ms=300, pad_ms=50
    )
    assert (cuts[0].start_ms, cuts[0].end_ms) == (350, 1200)


def test_silence_never_eats_a_kept_word() -> None:
    """The detector can over-report; a word inside the span still survives."""
    tokens = [(0, "甲", 0, 200), (1, "中间", 400, 700), (2, "乙", 900, 1100)]
    cuts = find_silence_cuts(
        tokens, duration_ms=1100, silences=[TimeRange(200, 900)], min_silence_ms=300, pad_ms=0
    )
    for cut in cuts:
        assert not (cut.start_ms < 700 and cut.end_ms > 400), cut


def test_short_word_between_two_silences_survives() -> None:
    """Rescript's PR #115 regression, asserted here.

    Two 0.4s pauses separated by a 60ms word: the pauses are 500ms apart, well
    inside MERGE_GAP, so a naive merge swallows the word. Our EDL would then
    drop the resulting 60ms keep-island and the word would vanish from the
    render while still showing in the transcript.
    """
    tokens = [
        (0, "前", 0, 1000),
        (1, "对", 1400, 1460),
        (2, "后", 1860, 2600),
    ]
    cuts = find_silence_cuts(
        tokens,
        duration_ms=2600,
        silences=[TimeRange(1000, 1400), TimeRange(1460, 1860)],
        min_silence_ms=300,
        pad_ms=0,
    )
    for cut in cuts:
        assert not (cut.start_ms < 1460 and cut.end_ms > 1400), f"切到了词上：{cut}"
    assert len(cuts) == 2


def test_nearby_silences_without_a_word_between_are_merged() -> None:
    tokens = [(0, "甲", 0, 500), (1, "乙", 900, 1400)]
    cuts = find_silence_cuts(
        tokens,
        duration_ms=1400,
        silences=[TimeRange(500, 650), TimeRange(700, 900)],
        min_silence_ms=100,
        pad_ms=0,
    )
    assert len(cuts) == 1


def test_nearby_silences_with_a_word_between_are_not_merged() -> None:
    tokens = [(0, "甲", 0, 500), (1, "的", 650, 700), (2, "乙", 900, 1400)]
    cuts = find_silence_cuts(
        tokens,
        duration_ms=1400,
        silences=[TimeRange(500, 650), TimeRange(700, 900)],
        min_silence_ms=100,
        pad_ms=0,
    )
    assert len(cuts) == 2


def test_silence_zero_duration_returns_nothing() -> None:
    assert find_silence_cuts(TOKENS, duration_ms=0, silences=[TimeRange(0, 100)]) == []


# --- disfluencies -------------------------------------------------------


def test_uncovered_speech_is_a_disfluency() -> None:
    """Speech the ASR dropped entirely — 400ms of 嗯 with no token on it."""
    tokens = [(0, "甲", 0, 500), (1, "乙", 1300, 1700)]
    cuts = find_disfluencies(
        tokens,
        duration_ms=1700,
        silences=[TimeRange(500, 600), TimeRange(1000, 1300)],
        min_duration_ms=300,
    )
    assert len(cuts) == 1
    # [600, 1000) is speech (not silence) with no token covering it.
    assert (cuts[0].start_ms, cuts[0].end_ms) == (600, 1000)


def test_short_uncovered_speech_is_not_a_disfluency() -> None:
    tokens = [(0, "甲", 0, 500), (1, "乙", 700, 1000)]
    cuts = find_disfluencies(
        tokens,
        duration_ms=1000,
        silences=[TimeRange(500, 650)],
        min_duration_ms=300,
    )
    assert cuts == []


def test_two_hundred_ms_of_uncovered_speech_is_still_too_short() -> None:
    """Below the threshold it is more likely timestamp slop than a dropped 嗯."""
    tokens = [(0, "甲", 0, 500), (1, "乙", 800, 1200)]
    cuts = find_disfluencies(
        tokens,
        duration_ms=1200,
        silences=[TimeRange(500, 600), TimeRange(700, 800)],
        min_duration_ms=300,
    )
    # [600, 700) is 100ms of uncovered speech — real, but too short to act on.
    assert cuts == []


def test_disfluency_needs_silences_to_define_speech() -> None:
    """With no silence list we cannot tell speech from silence, so propose none."""
    assert find_disfluencies(TOKENS, duration_ms=1300, silences=[]) == []


def test_deleted_words_do_not_cover_speech() -> None:
    """A word the user already cut leaves real audio; it should be proposed."""
    tokens = [(0, "甲", 0, 500), (1, "乙", 800, 1200)]
    cuts = find_disfluencies(
        tokens,
        duration_ms=1200,
        silences=[TimeRange(500, 800)],
        min_duration_ms=100,
    )
    assert cuts == []


# --- the whole plan -----------------------------------------------------


def test_build_plan_returns_all_three_kinds() -> None:
    plan = build_plan(
        PlanRequest(
            tokens=[
                (0, "甲", 0, 500),
                (1, "嗯", 500, 700),
                (2, "乙", 1300, 1800),
            ],
            duration_ms=1800,
            silences=[TimeRange(700, 1000), TimeRange(1000, 1300)],
            fillers=FILLERS,
            min_silence_ms=300,
            silence_pad_ms=0,
        )
    )
    assert len(plan.fillers) == 1
    assert plan.fillers[0].text == "嗯"
    assert isinstance(plan.summary()["silence"]["items"], int)


def test_plan_summary_counts_items_and_duration() -> None:
    plan = build_plan(
        PlanRequest(
            tokens=[(0, "甲", 0, 500), (1, "嗯", 500, 700), (2, "乙", 1300, 1800)],
            duration_ms=1800,
            silences=[TimeRange(700, 1300)],
            fillers=FILLERS,
            min_silence_ms=300,
            silence_pad_ms=0,
        )
    )
    summary = plan.summary()
    assert summary["filler"] == {"items": 1, "duration_ms": 200}


def test_selected_returns_time_order_across_kinds() -> None:
    plan = build_plan(
        PlanRequest(
            tokens=[(0, "嗯", 0, 200), (1, "甲", 200, 500)],
            duration_ms=1500,
            silences=[TimeRange(500, 1500)],
            fillers=FILLERS,
            min_silence_ms=300,
            silence_pad_ms=0,
        )
    )
    items = plan.selected({"filler", "silence"})
    assert [i.start_ms for i in items] == sorted(i.start_ms for i in items)


def test_selected_honours_the_ticked_kinds() -> None:
    plan = build_plan(
        PlanRequest(
            tokens=[(0, "嗯", 0, 200), (1, "甲", 200, 500)],
            duration_ms=1500,
            silences=[TimeRange(500, 1500)],
            fillers=FILLERS,
            min_silence_ms=300,
            silence_pad_ms=0,
        )
    )
    assert [i.kind for i in plan.selected({"filler"})] == ["filler"]
    assert plan.selected(set()) == ()


def test_empty_plan_is_an_empty_plan_not_an_error() -> None:
    plan = build_plan(PlanRequest())
    assert plan.selected({"filler", "silence", "disfluency"}) == ()


@pytest.mark.parametrize("kind", ["filler", "silence", "disfluency"])
def test_items_for_unknown_kind_is_empty(kind: str) -> None:
    plan = build_plan(PlanRequest())
    assert plan.items_for(kind) == ()


def test_cleanup_item_duration_cannot_be_negative() -> None:
    item = CleanupItem(kind="silence", start_ms=500, end_ms=200)
    assert item.duration_ms == -300  # dataclass stays dumb; the finders never emit this
