"""Timeline-preserving text repair (AGENTS.md §6.3).

These tests exist because the failure mode is silent: a correction that shifts
timestamps produces subtitles that are *wrong but plausible*, and nobody notices
until the cut lands in the middle of a word.

Since the editing unit is the word, a replacement is always **1 token → 1 token**
and the replacement text may be any length. That is the property these tests now
pin: the token's start and end are the ASR's observations and stay put no matter
what is typed, and neighbouring tokens never move.
"""

from __future__ import annotations

import pytest

from animatter.core.realign import (
    RealignError,
    TokenRange,
    apply_replacement,
    assert_monotonic,
    locate_sentence,
    locate_text,
    shift_ranges_after,
)
from animatter.core.tokens import TokenSpan, WordSpan, expand_words


def toks(*specs: tuple[str, int, int]) -> list[TokenSpan]:
    return [
        TokenSpan(
            idx=i,
            start_ms=s,
            end_ms=e,
            text=t,
            kind="word",
            src_word_idx=i,
            punc_after="",
            derived=False,
        )
        for i, (t, s, e) in enumerate(specs)
    ]


# --- locating -----------------------------------------------------------


def test_locate_exact_run() -> None:
    tokens = toks(("剧集", 0, 200), ("标签", 200, 400))
    assert locate_text(tokens, "剧集") == [TokenRange(0, 1)]


def test_locate_returns_empty_when_absent() -> None:
    tokens = toks(("剧集", 0, 100), ("标签", 100, 200))
    assert locate_text(tokens, "句级") == []


def test_locate_multiple_occurrences() -> None:
    tokens = toks(("剧集", 0, 100), ("中间", 100, 200), ("剧集", 200, 300))
    assert locate_text(tokens, "剧集") == [TokenRange(0, 1), TokenRange(2, 3)]


def test_locate_respects_bounds() -> None:
    tokens = toks(("剧集", 0, 100), ("标签", 100, 200))
    assert locate_text(tokens, "剧集", start=1) == []


def test_locate_empty_needle() -> None:
    assert locate_text(toks(("甲", 0, 1)), "") == []


def test_locate_inside_sentence() -> None:
    tokens = toks(("前", 0, 10), ("剧集", 10, 20), ("后", 30, 40))
    assert locate_sentence(tokens, 1, 2, "剧集") == TokenRange(1, 2)


def test_locate_outside_sentence_returns_none() -> None:
    tokens = toks(("剧集", 0, 10), ("标签", 10, 20))
    assert locate_sentence(tokens, 0, 1, "标签") is None


# --- word replacement: timing is pinned ---------------------------------


def test_replacement_keeps_the_word_timing() -> None:
    """1 token in, 1 token out: both edges are observed, so both are preserved."""
    tokens = toks(("剧集", 1000, 1200), ("标签", 1200, 1400))
    result = apply_replacement(tokens, TokenRange(0, 1), "句级")

    assert [t.text for t in result.tokens] == ["句级", "标签"]
    assert (result.tokens[0].start_ms, result.tokens[0].end_ms) == (1000, 1200)
    assert result.token_delta == 0


def test_neighbours_never_move() -> None:
    tokens = toks(("前", 0, 100), ("剧集", 100, 200), ("后", 200, 300))
    result = apply_replacement(tokens, TokenRange(1, 2), "句级")
    assert (result.tokens[0].start_ms, result.tokens[0].end_ms) == (0, 100)
    assert (result.tokens[2].start_ms, result.tokens[2].end_ms) == (200, 300)


def test_replacement_may_change_length_freely() -> None:
    """The headline consequence of word-level editing.

    Under character-level tokens a 2→4 character replacement redistributed the
    window and moved interior boundaries. With one token per word there is no
    interior, so the text length is unconstrained.
    """
    tokens = toks(("AI", 0, 200), ("后", 200, 400))

    shorter = apply_replacement(tokens, TokenRange(0, 1), "人工智能技术")
    assert shorter.tokens[0].text == "人工智能技术"
    assert (shorter.tokens[0].start_ms, shorter.tokens[0].end_ms) == (0, 200)
    assert shorter.token_delta == 0

    longer = apply_replacement(tokens, TokenRange(0, 1), "A")
    assert (longer.tokens[0].start_ms, longer.tokens[0].end_ms) == (0, 200)


def test_multi_word_range_collapses_to_one_token() -> None:
    tokens = toks(("句", 0, 100), ("级", 100, 200), ("标签", 200, 400))
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")
    assert len(result.tokens) == 2
    assert result.tokens[0].text == "句级"
    # The merged token must span the whole replaced window.
    assert (result.tokens[0].start_ms, result.tokens[0].end_ms) == (0, 200)
    assert result.token_delta == -1


def test_zero_duration_window_still_safe() -> None:
    tokens = toks(("甲", 100, 100), ("乙", 100, 200))
    result = apply_replacement(tokens, TokenRange(0, 1), "新词")
    assert result.tokens[0].start_ms == result.tokens[0].end_ms == 100
    assert_monotonic(result.tokens)


def test_indices_renumbered_densely() -> None:
    tokens = toks(("甲", 0, 10), ("乙", 10, 20), ("丙", 20, 30))
    result = apply_replacement(tokens, TokenRange(1, 2), "丁")
    assert [t.idx for t in result.tokens] == [0, 1, 2]


def test_timeline_stays_monotonic_after_replacement() -> None:
    tokens = toks(("A", 0, 300), ("B", 300, 600))
    result = apply_replacement(tokens, TokenRange(0, 1), "很长的一段替换文本也不会撑破时间轴")
    assert_monotonic(result.tokens)


# --- punctuation and provenance ----------------------------------------


def test_trailing_punctuation_moves_to_new_tail() -> None:
    tokens = [
        TokenSpan(0, 0, 100, "剧", "word", 0, "", False),
        TokenSpan(1, 100, 200, "集", "word", 0, "。", False),
    ]
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")
    assert result.tokens[-1].punc_after == "。"


def test_replacement_marked_derived() -> None:
    """Edited text was not observed, so its timing claim is weaker."""
    tokens = toks(("剧集", 0, 100))
    result = apply_replacement(tokens, TokenRange(0, 1), "句级")
    assert result.tokens[0].derived is True


# --- error handling -----------------------------------------------------


def test_empty_replacement_rejected() -> None:
    with pytest.raises(RealignError, match="替换文本为空"):
        apply_replacement(toks(("甲", 0, 10)), TokenRange(0, 1), "   ")


def test_out_of_bounds_range_rejected() -> None:
    with pytest.raises(RealignError, match="超出"):
        apply_replacement(toks(("甲", 0, 10)), TokenRange(0, 5), "乙")


def test_empty_range_rejected() -> None:
    with pytest.raises(RealignError, match="空的替换区间"):
        apply_replacement(toks(("甲", 0, 10)), TokenRange(0, 0), "乙")


# --- monotonic guard ----------------------------------------------------


def test_assert_monotonic_accepts_clean_stream() -> None:
    assert_monotonic(toks(("甲", 0, 100), ("乙", 100, 200)))


def test_assert_monotonic_rejects_overlap() -> None:
    with pytest.raises(RealignError, match="早于"):
        assert_monotonic(toks(("甲", 0, 150), ("乙", 100, 200)))


def test_assert_monotonic_rejects_inversion() -> None:
    with pytest.raises(RealignError, match="倒挂"):
        assert_monotonic(toks(("甲", 200, 100)))


# --- sentence range shifting -------------------------------------------


def test_shift_untouched_before_edit() -> None:
    assert shift_ranges_after([(0, 5)], at_token=5, delta=-2) == [(0, 5)]


def test_shift_containing_sentence_only_moves_end() -> None:
    # The edit sits inside sentence 0; its start must not move.
    assert shift_ranges_after([(0, 5), (5, 10)], at_token=2, delta=-1) == [(0, 4), (4, 9)]


def test_shift_following_sentences_move_both_ends() -> None:
    assert shift_ranges_after([(0, 2), (2, 5)], at_token=2, delta=-1) == [(0, 2), (2, 4)]


def test_shift_edit_at_sentence_start_keeps_its_start() -> None:
    """The edit sits on the sentence's first token, so only its tail moves.

    Regression: treating that case as "starts after the edit" shifted the start
    too, which pushed the edit out of every range and broke INV-1.
    """
    assert shift_ranges_after([(0, 3), (3, 6)], at_token=3, delta=-1) == [(0, 3), (3, 5)]


# --- end to end ---------------------------------------------------------


def test_word_level_expansion_then_repair() -> None:
    """Realistic flow: ASR words → tokens → fix one word → timings unchanged."""
    words = [
        WordSpan("今天", 0, 760),
        WordSpan("我们", 760, 1280),
        WordSpan("聊聊", 1280, 1960, punctuation="。"),
    ]
    tokens = expand_words(words)
    assert len(tokens) == 3

    result = apply_replacement(tokens, TokenRange(2, 3), "讨论一下")

    assert [t.text for t in result.tokens] == ["今天", "我们", "讨论一下"]
    # Every timing is exactly what the ASR reported.
    assert [(t.start_ms, t.end_ms) for t in result.tokens] == [
        (0, 760),
        (760, 1280),
        (1280, 1960),
    ]
    assert result.tokens[-1].punc_after == "。"
