"""Filler-list parsing and matching.

The failure this file exists to prevent is not "a filler was missed" — that
costs one manual delete. It is "a real word was deleted", which costs the audio
and the user's trust in the button. So the tests are weighted towards what the
default list must *refuse*.
"""

from __future__ import annotations

import pytest

from animatter.core.fillers import (
    DEFAULT_FILLERS,
    FORBIDDEN_DEFAULTS,
    format_word_list,
    is_filler,
    merge_adjacent_fillers,
    normalize,
    parse_word_list,
)

DEFAULTS = frozenset(DEFAULT_FILLERS)


# --- normalisation ------------------------------------------------------


def test_normalize_strips_edge_punctuation() -> None:
    assert normalize("嗯，") == "嗯"
    assert normalize("「呃」") == "呃"
    assert normalize("  Um!  ") == "um"


def test_normalize_case_folds_and_folds_width() -> None:
    # Full-width Latin arrives from copy-pasted subtitles.
    assert normalize("ＵＭ") == "um"
    assert normalize("Uhh") == "uhh"


def test_normalize_keeps_interior_hyphen() -> None:
    """`mm-hmm` is one filler; splitting it would match two wrong tokens."""
    assert normalize("mm-hmm") == "mm-hmm"


def test_normalize_returns_empty_for_punctuation_only() -> None:
    assert normalize("……") == ""
    assert normalize("？！") == ""


# --- matching -----------------------------------------------------------


def test_detects_chinese_fillers() -> None:
    for word in ("嗯", "呃", "额", "唔"):
        assert is_filler(word, DEFAULTS), word


def test_detects_punctuated_filler() -> None:
    assert is_filler("嗯，", DEFAULTS)


def test_refuses_ordinary_words() -> None:
    for word in ("我们", "这个", "就是", "然后", "今天", "对", "好"):
        assert not is_filler(word, DEFAULTS), word


def test_refuses_ambiguous_discourse_markers() -> None:
    """These are the ones users ask to add — they must not ship by default."""
    for word in ("就是说", "那个", "然后呢", "你知道", "like", "so", "right", "you know"):
        assert not is_filler(word, DEFAULTS), word


def test_no_default_is_a_forbidden_term() -> None:
    offenders = DEFAULTS & FORBIDDEN_DEFAULTS
    assert offenders == set(), f"这些词是正常用词，不能进默认表：{offenders}"


def test_defaults_are_all_normalised() -> None:
    """A default that does not survive normalisation can never match."""
    for term in DEFAULT_FILLERS:
        assert normalize(term) == term, term


def test_user_list_can_add_a_discourse_marker() -> None:
    custom = DEFAULTS | {"就是说"}
    assert is_filler("就是说，", custom)


def test_empty_list_matches_nothing() -> None:
    assert not is_filler("嗯", frozenset())


# --- parsing the settings textarea --------------------------------------


def test_parse_one_per_line() -> None:
    assert parse_word_list("嗯\n呃\n唔") == ["嗯", "呃", "唔"]


def test_parse_accepts_commas_and_chinese_commas() -> None:
    assert parse_word_list("嗯，呃、唔,um") == ["嗯", "呃", "唔", "um"]


def test_parse_deduplicates_case_insensitively() -> None:
    assert parse_word_list("Um\num\nUM") == ["um"]


def test_parse_normalises_each_term() -> None:
    assert parse_word_list("「嗯」，") == ["嗯"]


def test_parse_ignores_blank_lines() -> None:
    assert parse_word_list("\n\n嗯\n   \n") == ["嗯"]


def test_parse_preserves_user_order() -> None:
    """Order is the only signal the user has; sorting it would hide it."""
    assert parse_word_list("唔\n嗯\n呃") == ["唔", "嗯", "呃"]


def test_format_round_trips_through_parse() -> None:
    terms = ["嗯", "呃", "um"]
    assert parse_word_list(format_word_list(terms)) == terms


# --- merging adjacent runs ----------------------------------------------


def _match(index: int, text: str, start: int, end: int):
    from animatter.core.fillers import FillerMatch

    return FillerMatch(token_index=index, text=text, start_ms=start, end_ms=end)


def test_three_consecutive_fillers_become_one_cut() -> None:
    runs = merge_adjacent_fillers([_match(1, "嗯", 100, 200), _match(2, "嗯", 200, 300)])
    assert len(runs) == 1
    assert runs[0].text == "嗯嗯"
    assert runs[0].token_indices == (1, 2)
    assert (runs[0].start_ms, runs[0].end_ms) == (100, 300)


def test_non_adjacent_fillers_stay_separate() -> None:
    runs = merge_adjacent_fillers([_match(1, "嗯", 100, 200), _match(3, "嗯", 500, 600)])
    assert len(runs) == 2


def test_merge_handles_empty_input() -> None:
    assert merge_adjacent_fillers([]) == []


def test_merge_sorts_unordered_input() -> None:
    runs = merge_adjacent_fillers([_match(2, "嗯", 200, 300), _match(1, "嗯", 100, 200)])
    assert len(runs) == 1
    assert runs[0].token_indices == (1, 2)


@pytest.mark.parametrize("text", ["嗯嗯嗯嗯嗯", "ummm"])
def test_long_runs_survive(text: str) -> None:
    matches = [_match(i, "嗯" if text.startswith("嗯") else "um", i * 100, (i + 1) * 100) for i in range(5)]
    runs = merge_adjacent_fillers(matches)
    assert len(runs) == 1
    assert runs[0].token_indices == (0, 1, 2, 3, 4)
