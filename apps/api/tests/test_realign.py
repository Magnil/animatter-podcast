"""Timeline-preserving text repair (AGENT.md §6.3).

These tests exist because the failure mode is silent: a correction that shifts
timestamps produces subtitles that are *wrong but plausible*, and nobody notices
until the cut lands in the middle of a word.
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
            idx=i, start_ms=s, end_ms=e, text=t, kind="char", src_word_idx=0, punc_after="", derived=True
        )
        for i, (t, s, e) in enumerate(specs)
    ]


# --- locating -----------------------------------------------------------


def test_locate_exact_run() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200), ("标", 200, 300), ("签", 300, 400))
    matches = locate_text(tokens, "剧集")
    assert matches == [TokenRange(0, 2)]


def test_locate_returns_empty_when_absent() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200))
    assert locate_text(tokens, "句级") == []


def test_locate_multiple_occurrences() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200), ("剧", 200, 300), ("集", 300, 400))
    assert locate_text(tokens, "剧集") == [TokenRange(0, 2), TokenRange(2, 4)]


def test_locate_respects_bounds() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200))
    assert locate_text(tokens, "剧集", start=1) == []


def test_locate_empty_needle() -> None:
    assert locate_text(toks(("甲", 0, 1)), "") == []


def test_locate_inside_sentence() -> None:
    tokens = toks(("前", 0, 10), ("剧", 10, 20), ("集", 20, 30), ("后", 30, 40))
    assert locate_sentence(tokens, 1, 3, "剧集") == TokenRange(1, 3)


def test_locate_outside_sentence_returns_none() -> None:
    tokens = toks(("剧", 0, 10), ("集", 10, 20))
    assert locate_sentence(tokens, 0, 1, "剧集") is None


# --- same-length replacement -------------------------------------------


def test_same_length_preserves_timings_exactly() -> None:
    tokens = toks(("剧", 1000, 1100), ("集", 1100, 1200), ("标", 1200, 1300), ("签", 1300, 1400))
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")

    assert [t.text for t in result.tokens] == ["句", "级", "标", "签"]
    assert [(t.start_ms, t.end_ms) for t in result.tokens[:2]] == [(1000, 1100), (1100, 1200)]
    assert result.token_delta == 0


def test_same_length_keeps_neighbours_untouched() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200), ("后", 200, 300))
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")
    assert (result.tokens[2].start_ms, result.tokens[2].end_ms) == (200, 300)


def test_indices_renumbered_densely() -> None:
    tokens = toks(("甲", 0, 10), ("乙", 10, 20), ("丙", 20, 30))
    result = apply_replacement(tokens, TokenRange(1, 2), "丁")
    assert [t.idx for t in result.tokens] == [0, 1, 2]


# --- different-length replacement --------------------------------------


def test_shrink_keeps_window_boundaries() -> None:
    # 4 chars → 1 token, must still occupy exactly [0, 400).
    tokens = toks(("人", 0, 100), ("工", 100, 200), ("智", 200, 300), ("能", 300, 400), ("后", 400, 500))
    result = apply_replacement(tokens, TokenRange(0, 4), "AI")

    assert result.tokens[0].text == "AI"
    assert result.tokens[0].start_ms == 0
    assert result.tokens[0].end_ms == 400
    # The following token must not move.
    assert result.tokens[1].start_ms == 400
    assert result.token_delta == -3


def test_expand_keeps_window_boundaries() -> None:
    tokens = toks(("A", 0, 200), ("B", 200, 400))
    result = apply_replacement(tokens, TokenRange(0, 1), "人工智能")

    assert "".join(t.text for t in result.tokens[:4]) == "人工智能"
    assert result.tokens[0].start_ms == 0
    assert result.tokens[3].end_ms == 200
    assert result.tokens[4].start_ms == 200  # following token untouched
    assert result.token_delta == 3


def test_expand_timings_are_monotonic() -> None:
    tokens = toks(("A", 0, 300), ("B", 300, 600))
    result = apply_replacement(tokens, TokenRange(0, 1), "很长的一段替换文本")
    assert_monotonic(result.tokens)


def test_zero_duration_window_still_safe() -> None:
    tokens = toks(("甲", 100, 100), ("乙", 100, 200))
    result = apply_replacement(tokens, TokenRange(0, 1), "新词")
    assert all(t.start_ms == t.end_ms == 100 for t in result.tokens[:2])


# --- punctuation and provenance ----------------------------------------


def test_trailing_punctuation_moves_to_new_tail() -> None:
    tokens = [
        TokenSpan(0, 0, 100, "剧", "char", 0, "", True),
        TokenSpan(1, 100, 200, "集", "char", 0, "。", True),
    ]
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")
    assert result.tokens[-1].punc_after == "。"


def test_replacement_marked_derived() -> None:
    tokens = toks(("剧", 0, 100), ("集", 100, 200))
    result = apply_replacement(tokens, TokenRange(0, 2), "句级")
    assert all(t.derived for t in result.tokens[:2])


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
    # Edit at token 2, which is the FIRST token of sentence 1, so sentence 1
    # absorbs the growth. Moving its start to 5 would leave [2,5) belonging to
    # no sentence at all — an INV-1 gap.
    assert shift_ranges_after([(0, 2), (2, 6)], at_token=2, delta=3) == [(0, 2), (2, 9)]


def test_shift_inside_sentence_keeps_start() -> None:
    assert shift_ranges_after([(0, 6), (6, 10)], at_token=3, delta=-2) == [(0, 4), (4, 8)]


def test_shift_at_boundary_belongs_to_earlier_sentence() -> None:
    # Token 3 starts sentence 1, so sentence 1 absorbs the change; sentence 0
    # ends exactly at the edit point and stays put.
    assert shift_ranges_after([(0, 3), (3, 7)], at_token=3, delta=1) == [(0, 3), (3, 8)]


def test_shift_zero_delta_is_identity() -> None:
    original = [(0, 3), (3, 7)]
    assert shift_ranges_after(original, at_token=3, delta=0) == original


# --- realistic end-to-end ------------------------------------------------


def test_realistic_asr_expansion_then_repair() -> None:
    words = [
        WordSpan("讨论", 0, 400),
        WordSpan("剧集标签", 400, 1600),
        WordSpan("体系", 1600, 2000),
    ]
    tokens = expand_words(words)
    assert len(tokens) == 8

    matches = locate_text(tokens, "剧集")
    assert len(matches) == 1
    span = matches[0]

    result = apply_replacement(tokens, span, "句级")
    assert "".join(t.text for t in result.tokens) == "讨论句级标签体系"

    # The whole stream must still span the original audio duration.
    assert result.tokens[0].start_ms == 0
    assert result.tokens[-1].end_ms == 2000
    assert_monotonic(result.tokens)


def test_same_length_replacement_keeps_interior_boundaries() -> None:
    """Replacing text with the same character count must not move any boundary.

    Regression: re-deriving interior timings shifted 14960→15000 for a 2-char
    swap, so the editor's cut point drifted even though the audio was unchanged.
    """
    tokens = toks(("句", 14800, 14960), ("级", 14960, 15200), ("标", 15200, 15460))
    result = apply_replacement(tokens, TokenRange(0, 2), "句集")

    assert [(t.start_ms, t.end_ms) for t in result.tokens] == [
        (14800, 14960),
        (14960, 15200),
        (15200, 15460),
    ]
    assert "".join(t.text for t in result.tokens) == "句集标"


def test_uneven_word_boundaries_preserved_exactly() -> None:
    # Real ASR words have uneven per-character durations; a same-length edit
    # must reproduce them rather than redistributing evenly.
    tokens = toks(("一", 0, 180), ("二", 180, 400), ("三", 400, 1000))
    result = apply_replacement(tokens, TokenRange(0, 3), "甲乙丙")
    assert [(t.start_ms, t.end_ms) for t in result.tokens] == [
        (0, 180),
        (180, 400),
        (400, 1000),
    ]
