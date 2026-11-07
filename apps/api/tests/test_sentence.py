"""Sentence range algebra (AGENT.md §6.4, INV-1..4).

The editor's 「另起一句 / 并入上句 / 并入下句」 actions all reduce to the
operations exercised here. Every test asserts the invariants hold afterwards,
because a violated invariant corrupts the timeline silently.
"""

from __future__ import annotations

import pytest

from animatter.core.sentence import (
    InvalidBoundaryError,
    SentenceSpan,
    build_from_asr,
    extend_into,
    merge_range,
    split_at,
    validate,
)


def spans(*pairs: tuple[int, int]) -> list[SentenceSpan]:
    return [SentenceSpan(ord=i, token_start=s, token_end=e) for i, (s, e) in enumerate(pairs)]


# --- build_from_asr -----------------------------------------------------


def test_build_covers_whole_stream() -> None:
    result = build_from_asr([(0, 3, 0), (3, 7, 1)], 7)
    validate(result, 7)
    assert [s.ord for s in result] == [0, 1]


def test_build_closes_gaps_by_growing_previous() -> None:
    # Tokens 3-4 were claimed by no ASR sentence (e.g. punctuation-only words
    # that the tokeniser dropped). They join the preceding sentence.
    result = build_from_asr([(0, 3, None), (5, 7, None)], 7)
    validate(result, 7)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 5), (5, 7)]


def test_build_drops_empty_sentences() -> None:
    result = build_from_asr([(0, 2, None), (2, 2, None)], 4)
    validate(result, 4)
    assert len(result) == 1
    assert result[0].token_end == 4


def test_build_handles_no_asr_sentences() -> None:
    result = build_from_asr([], 10)
    assert len(result) == 1
    validate(result, 10)


def test_build_handles_zero_tokens() -> None:
    assert build_from_asr([], 0) == []


# --- validate -----------------------------------------------------------


def test_validate_detects_gap() -> None:
    with pytest.raises(InvalidBoundaryError, match="gap"):
        validate(spans((0, 2), (3, 5)), 5)


def test_validate_detects_overlap() -> None:
    with pytest.raises(InvalidBoundaryError, match="overlap"):
        validate(spans((0, 3), (2, 5)), 5)


def test_validate_detects_short_tail() -> None:
    with pytest.raises(InvalidBoundaryError, match="last sentence ends"):
        validate(spans((0, 2)), 5)


def test_validate_detects_empty_sentence() -> None:
    with pytest.raises(InvalidBoundaryError, match="empty sentence"):
        validate(spans((0, 2), (2, 2)), 2)


def test_validate_requires_at_least_one() -> None:
    with pytest.raises(InvalidBoundaryError, match="no sentences"):
        validate([], 0)


# --- split_at -----------------------------------------------------------


def test_split_inside_sentence() -> None:
    result = split_at(spans((0, 6)), 3)
    validate(result, 6)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 3), (3, 6)]
    assert result[1].origin == "manual_split"


def test_split_renumbers() -> None:
    result = split_at(spans((0, 4), (4, 8)), 2)
    assert [s.ord for s in result] == [0, 1, 2]


@pytest.mark.parametrize("index", [0, 3, -1])
def test_split_at_existing_boundary_rejected(index: int) -> None:
    with pytest.raises(InvalidBoundaryError):
        split_at(spans((0, 3), (3, 6)), index)


def test_split_outside_stream_rejected() -> None:
    with pytest.raises(InvalidBoundaryError):
        split_at(spans((0, 3)), 99)


# --- merge_range --------------------------------------------------------


def test_merge_three_sentences() -> None:
    result = merge_range(spans((0, 2), (2, 4), (4, 6), (6, 8)), 1, 5)
    validate(result, 8)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 6), (6, 8)]
    assert result[0].origin == "manual_merge"


def test_merge_refuses_two_different_speakers() -> None:
    """A sentence carries one speaker, so merging two is a forced choice.

    Picking either one is wrong: the merged row would attribute one person's
    words to the other. Refusing is the only answer that keeps the transcript
    honest, and the editor reports it rather than silently dropping a label.
    """
    original = [
        SentenceSpan(0, 0, 2, speaker_id=0),
        SentenceSpan(1, 2, 4, speaker_id=1),
    ]
    with pytest.raises(InvalidBoundaryError, match="SPEAKER-1"):
        merge_range(original, 0, 4)


def test_merge_keeps_the_one_annotation_it_has() -> None:
    """Unannotated neighbours merge freely, and the label survives.

    Keeping "whatever the first sentence had" would drop a label the user had
    just set on the second one — which is a silent data loss, not a merge.
    """
    original = [
        SentenceSpan(0, 0, 2, speaker_id=None),
        SentenceSpan(1, 2, 4, speaker_id=1),
    ]
    result = merge_range(original, 0, 4)
    assert len(result) == 1
    assert result[0].speaker_id == 1


def test_merge_of_two_unannotated_sentences_has_no_speaker() -> None:
    result = merge_range(spans((0, 2), (2, 4)), 0, 4)
    assert len(result) == 1
    assert result[0].speaker_id is None


def test_merge_empty_range_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="empty"):
        merge_range(spans((0, 4)), 2, 2)


def test_merge_range_outside_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="touches no sentence"):
        merge_range(spans((0, 4)), 100, 200)


# --- extend_into (「并入上句 / 并入下句」) ------------------------------


def test_extend_into_prev_moves_selection_up() -> None:
    # Selection [2,4) is the head of the second sentence; move it into the first.
    result = extend_into(spans((0, 2), (2, 6)), 2, 4, direction="prev")
    validate(result, 6)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 4), (4, 6)]


def test_extend_into_next_moves_tail_down() -> None:
    # Selection [2,4) is the tail of the first sentence; move it into the second.
    result = extend_into(spans((0, 4), (4, 8)), 2, 4, direction="next")
    validate(result, 8)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 2), (2, 8)]


def test_extend_into_prev_moves_head_up() -> None:
    # Selection [4,6) is the head of the second sentence; move it into the first.
    result = extend_into(spans((0, 4), (4, 8)), 4, 6, direction="prev")
    validate(result, 8)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 6), (6, 8)]


def test_extend_into_prev_at_first_sentence_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="no previous"):
        extend_into(spans((0, 4)), 1, 2, direction="prev")


def test_extend_into_next_at_last_sentence_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="no next"):
        extend_into(spans((0, 4)), 1, 2, direction="next")


def test_extend_whole_sentence_prev_merges() -> None:
    """Selecting a whole sentence and joining upward is how users merge two sentences."""
    result = extend_into(spans((0, 2), (2, 4)), 2, 4, direction="prev")
    validate(result, 4)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 4)]


def test_extend_whole_sentence_next_merges() -> None:
    result = extend_into(spans((0, 2), (2, 4)), 0, 2, direction="next")
    validate(result, 4)
    assert [(s.token_start, s.token_end) for s in result] == [(0, 4)]


def test_extend_whole_first_sentence_prev_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="no previous"):
        extend_into(spans((0, 2), (2, 4)), 0, 2, direction="prev")


def test_extend_whole_last_sentence_next_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="no next"):
        extend_into(spans((0, 2), (2, 4)), 2, 4, direction="next")


def test_extend_empty_selection_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="empty"):
        extend_into(spans((0, 2), (2, 4)), 1, 1, direction="prev")


def test_extend_unknown_direction_rejected() -> None:
    with pytest.raises(InvalidBoundaryError, match="unknown direction"):
        extend_into(spans((0, 4)), 1, 2, direction="sideways")


def test_extend_split_then_merge_roundtrip() -> None:
    """另起一句 then 并入上句 must return to the original shape."""
    original = spans((0, 6))
    split = split_at(original, 3)
    merged = extend_into(split, split[1].token_start, split[1].token_end, direction="prev")
    # The second sentence became empty; extend_into drops empties.
    assert [(s.token_start, s.token_end) for s in merged] == [(0, 6)]
