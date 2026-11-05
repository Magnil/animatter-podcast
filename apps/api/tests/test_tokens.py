"""Word → token expansion (AGENT.md §6.3).

These tests pin the *arithmetic*, not just the happy path: character-level
timestamps are interpolated, so an off-by-one here shifts every downstream
subtitle and every cut point.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from itertools import pairwise

import pytest

from animatter.core.tokens import TokenSpan, WordSpan, expand_words, looks_like_sentence_end


def test_single_char_word_keeps_exact_time() -> None:
    tokens = expand_words([WordSpan("好", 100, 300)])
    assert len(tokens) == 1
    assert (tokens[0].start_ms, tokens[0].end_ms) == (100, 300)
    assert tokens[0].derived is False


def test_cjk_word_splits_per_char_with_contiguous_spans() -> None:
    tokens = expand_words([WordSpan("今天我们来聊", 1000, 1600)])

    assert [t.text for t in tokens] == ["今", "天", "我", "们", "来", "聊"]
    assert all(t.derived for t in tokens)

    # Contiguity: each token starts exactly where the previous ended.
    for previous, current in pairwise(tokens):
        assert previous.end_ms == current.start_ms

    assert tokens[0].start_ms == 1000
    assert tokens[-1].end_ms == 1600


def test_ascii_run_inside_cjk_stays_whole() -> None:
    tokens = expand_words([WordSpan("用AI做字幕", 0, 500)])
    assert [t.text for t in tokens] == ["用", "AI", "做", "字", "幕"]
    # The ASCII unit is a 'word' token, not a 'char' token.
    assert tokens[1].kind == "word"


def test_english_word_is_not_split() -> None:
    tokens = expand_words([WordSpan("Kubernetes", 0, 800)])
    assert len(tokens) == 1
    assert tokens[0].text == "Kubernetes"
    assert tokens[0].derived is False


def test_punctuation_attaches_to_last_token_of_word() -> None:
    tokens = expand_words([WordSpan("你好", 0, 400, punctuation="，")])
    assert tokens[-1].punc_after == "，"
    assert all(t.punc_after == "" for t in tokens[:-1])


def test_punctuation_on_whitespace_word_moves_to_previous() -> None:
    tokens = expand_words(
        [
            WordSpan("结束", 0, 400),
            WordSpan(" ", 400, 400, punctuation="。"),
        ]
    )
    assert [t.text for t in tokens] == ["结", "束"]
    assert tokens[-1].punc_after == "。"


def test_inverted_timestamps_are_normalised() -> None:
    tokens = expand_words([WordSpan("倒挂", 900, 100)])
    assert tokens[0].start_ms <= tokens[0].end_ms
    assert tokens[0].start_ms == 100


def test_zero_length_word_yields_zero_length_tokens() -> None:
    tokens = expand_words([WordSpan("零长", 500, 500)])
    assert len(tokens) == 2
    assert all(t.start_ms == t.end_ms == 500 for t in tokens)


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
