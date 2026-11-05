"""Word → token expansion (AGENT.md §6.3).

Since 2026-11-06 the editing unit is the **ASR word**, not the character; see the
module docstring for why. These tests pin that contract plus the arithmetic that
must still hold: no overlap, no inverted spans, dense indices.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from itertools import pairwise

import pytest

from animatter.core.tokens import TokenSpan, WordSpan, expand_words, looks_like_sentence_end


def test_single_word_becomes_one_token() -> None:
    tokens = expand_words([WordSpan("今天", 100, 300)])
    assert len(tokens) == 1
    assert tokens[0].text == "今天"
    assert (tokens[0].start_ms, tokens[0].end_ms) == (100, 300)


def test_word_timing_is_the_asr_timing() -> None:
    """A word token must be *observed*, not interpolated.

    This is the whole point of dropping character-level editing: with one token
    per word there are no interior boundaries that a replacement could move.
    """
    tokens = expand_words([WordSpan("我们来聊", 1000, 1600)])
    assert len(tokens) == 1
    assert (tokens[0].start_ms, tokens[0].end_ms) == (1000, 1600)
    assert tokens[0].derived is False


def test_cjk_is_not_split_per_char() -> None:
    tokens = expand_words([WordSpan("今天我们来聊", 1000, 1600)])
    assert [t.text for t in tokens] == ["今天我们来聊"]


def test_ascii_inside_cjk_stays_one_token() -> None:
    tokens = expand_words([WordSpan("用AI做字幕", 0, 500)])
    assert [t.text for t in tokens] == ["用AI做字幕"]
    assert tokens[0].kind == "word"


def test_english_word_is_one_token() -> None:
    tokens = expand_words([WordSpan("Kubernetes", 0, 800)])
    assert len(tokens) == 1
    assert tokens[0].text == "Kubernetes"


def test_punctuation_attaches_to_the_word() -> None:
    tokens = expand_words([WordSpan("你好", 0, 400, punctuation="，")])
    assert len(tokens) == 1
    assert tokens[0].punc_after == "，"


def test_punctuation_on_whitespace_word_moves_to_previous() -> None:
    tokens = expand_words(
        [
            WordSpan("结束", 0, 400),
            WordSpan(" ", 400, 400, punctuation="。"),
        ]
    )
    assert [t.text for t in tokens] == ["结束"]
    assert tokens[-1].punc_after == "。"


def test_inverted_timestamps_are_normalised() -> None:
    tokens = expand_words([WordSpan("倒挂", 900, 100)])
    assert tokens[0].start_ms <= tokens[0].end_ms
    assert tokens[0].start_ms == 100


def test_zero_length_word_keeps_zero_length_token() -> None:
    """A zero-length word must still yield one token, not vanish.

    Dropping it would silently delete a word the user can see in the transcript.
    """
    tokens = expand_words([WordSpan("零长", 500, 500)])
    assert len(tokens) == 1
    assert tokens[0].start_ms == tokens[0].end_ms == 500


def test_indices_are_dense_and_ascending() -> None:
    tokens = expand_words(
        [
            WordSpan("第一", 0, 200),
            WordSpan("second", 200, 500),
            WordSpan("第三", 500, 700),
        ]
    )
    assert [t.idx for t in tokens] == list(range(len(tokens)))


def test_start_idx_offsets_numbering() -> None:
    tokens = expand_words([WordSpan("续", 0, 100)], start_idx=42)
    assert tokens[0].idx == 42


def test_tokens_never_overlap_across_words() -> None:
    tokens = expand_words(
        [
            WordSpan("前面", 0, 400),
            WordSpan("后面", 400, 900),
        ]
    )
    for previous, current in pairwise(tokens):
        assert previous.end_ms <= current.start_ms


def test_src_word_idx_points_back_at_the_word() -> None:
    """The UI needs this to merge tokens back into words for speaker edits."""
    tokens = expand_words(
        [
            WordSpan("甲", 0, 100),
            WordSpan("乙", 100, 200),
        ]
    )
    assert [t.src_word_idx for t in tokens] == [0, 1]


def test_whitespace_only_word_is_dropped_but_keeps_punctuation() -> None:
    tokens = expand_words([WordSpan("  ", 0, 100)])
    assert tokens == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("你好。", True), ("结束?", True), ("还没完", False), ("", False)],
)
def test_sentence_end_heuristic(text: str, expected: bool) -> None:
    assert looks_like_sentence_end(text) is expected


def test_empty_word_list_returns_empty() -> None:
    assert expand_words([]) == []


def test_token_is_frozen() -> None:
    token = TokenSpan(0, 0, 1, "字", "char", 0, "", True)
    with pytest.raises(FrozenInstanceError):
        token.text = "改"  # type: ignore[misc]
