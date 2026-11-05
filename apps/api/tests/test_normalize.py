"""ASR result normalisation: offsets, merging, and invariant preservation."""

from __future__ import annotations

import pytest

from animatter.core.sentence import validate
from animatter.providers.asr.base import AsrChannel, AsrResult, AsrSentence, AsrWord
from animatter.services.normalize import (
    NormalizationError,
    merge_segments,
    normalize,
    normalize_channel,
)


def sentence(
    start: int, end: int, words: list[tuple[str, int, int]], speaker: int | None = None
) -> AsrSentence:
    return AsrSentence(
        start_ms=start,
        end_ms=end,
        text="".join(w[0] for w in words),
        speaker_id=speaker,
        words=[AsrWord(text=w[0], start_ms=w[1], end_ms=w[2]) for w in words],
    )


def channel(*sentences: AsrSentence, channel_id: int = 0) -> AsrChannel:
    return AsrChannel(channel_id=channel_id, sentences=list(sentences), full_text="")


def result(*channels: AsrChannel, duration_ms: int = 10_000) -> AsrResult:
    return AsrResult(duration_ms=duration_ms, channels=list(channels), raw={})


def test_normalize_produces_valid_sentence_ranges() -> None:
    payload = result(
        channel(
            sentence(0, 600, [("你好", 0, 300), ("世界", 300, 600)], speaker=0),
            sentence(600, 1200, [("再见", 600, 1200)], speaker=1),
        )
    )
    normalized = normalize(payload)
    validate(normalized.sentences, len(normalized.tokens))
    assert len(normalized.sentences) == 2
    assert [s.speaker_id for s in normalized.sentences] == [0, 1]


def test_offset_shifts_every_timestamp() -> None:
    payload = result(channel(sentence(0, 400, [("开头", 0, 400)])))
    shifted = normalize(payload, offset_ms=60_000)
    assert shifted.tokens[0].start_ms == 60_000
    assert shifted.tokens[-1].end_ms == 60_400


def test_offset_preserves_relative_spacing() -> None:
    payload = result(
        channel(
            sentence(0, 200, [("甲", 0, 200)]),
            sentence(200, 900, [("乙", 200, 900)]),
        )
    )
    plain = normalize(payload)
    shifted = normalize(payload, offset_ms=30_000)
    assert shifted.tokens[-1].end_ms - shifted.tokens[0].start_ms == (
        plain.tokens[-1].end_ms - plain.tokens[0].start_ms
    )


def test_duration_is_derived_from_last_token() -> None:
    payload = result(channel(sentence(0, 750, [("时长", 0, 750)])), duration_ms=999_999)
    assert normalize(payload).duration_ms == 750


def test_empty_sentences_are_dropped_not_fatal() -> None:
    payload = result(
        channel(
            sentence(0, 300, [("有字", 0, 300)]),
            AsrSentence(start_ms=300, end_ms=300, text="", speaker_id=None, words=[]),
        )
    )
    normalized = normalize(payload)
    validate(normalized.sentences, len(normalized.tokens))
    assert len(normalized.sentences) == 1


def test_no_tokens_raises() -> None:
    payload = result(channel(AsrSentence(0, 0, "", None, [])))
    with pytest.raises(NormalizationError):
        normalize(payload)


def test_no_channels_raises() -> None:
    with pytest.raises(NormalizationError):
        normalize(result())


def test_picks_requested_channel() -> None:
    left = channel(sentence(0, 300, [("左", 0, 300)]), channel_id=0)
    right = channel(sentence(0, 300, [("右", 0, 300)]), channel_id=1)
    payload = result(left, right)
    assert normalize(payload, channel=1).tokens[0].text == "右"
    assert normalize(payload, channel=0).tokens[0].text == "左"


def test_normalize_channel_directly() -> None:
    normalized = normalize_channel(channel(sentence(0, 200, [("直接", 0, 200)])))
    assert len(normalized.tokens) == 2


# --- merge_segments -----------------------------------------------------


def test_merge_single_part_is_identity() -> None:
    part = normalize(result(channel(sentence(0, 200, [("单段", 0, 200)]))))
    assert merge_segments([part]) is part


def test_merge_two_parts_renumbers_correctly() -> None:
    first = normalize(result(channel(sentence(0, 400, [("前段", 0, 400)]))))
    second = normalize(result(channel(sentence(0, 400, [("后段", 0, 400)]))), offset_ms=10_000)
    merged = merge_segments([first, second])

    validate(merged.sentences, len(merged.tokens))
    assert [t.idx for t in merged.tokens] == list(range(len(merged.tokens)))
    assert len(merged.sentences) == 2
    assert merged.sentences[1].token_start == len(first.tokens)
    # "后段" is two chars spanning 0-400ms locally, so the second char starts at 10200.
    assert merged.tokens[-1].start_ms == 10_200


def test_merge_preserves_time_order_across_seam() -> None:
    first = normalize(result(channel(sentence(0, 1_000, [("甲甲", 0, 1_000)]))))
    second = normalize(result(channel(sentence(0, 1_000, [("乙乙", 0, 1_000)]))), offset_ms=1_100)
    merged = merge_segments([first, second])
    for previous, current in zip(merged.tokens, merged.tokens[1:], strict=False):
        assert previous.end_ms <= current.start_ms


def test_merge_three_parts_keeps_all_sentences() -> None:
    parts = [
        normalize(result(channel(sentence(0, 200, [("一", 0, 200)]))), offset_ms=offset)
        for offset in (0, 1_000, 2_000)
    ]
    merged = merge_segments(parts)
    validate(merged.sentences, len(merged.tokens))
    assert len(merged.sentences) == 3


def test_merge_empty_list_raises() -> None:
    with pytest.raises(NormalizationError):
        merge_segments([])
