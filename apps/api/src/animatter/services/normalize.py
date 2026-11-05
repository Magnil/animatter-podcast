"""ASR result → database rows.

The normalisation step is where the two most consequential conversions happen:

1. **Word → token expansion** (``core.tokens``), which manufactures the
   character-level timestamps the editor needs.
2. **Segment offsetting**, where per-chunk results get their ``segment_start_ms``
   added back to land on the original timeline (AGENT.md §6.2).

Both are pure and therefore tested without any network.
"""

from __future__ import annotations

from dataclasses import dataclass

from animatter.core.sentence import SentenceSpan, build_from_asr, validate
from animatter.core.tokens import TokenSpan, expand_words
from animatter.providers.asr.base import AsrChannel, AsrResult, AsrSentence


@dataclass(frozen=True)
class NormalizedTranscript:
    """Everything one transcript version needs, ready to persist."""

    tokens: list[TokenSpan]
    sentences: list[SentenceSpan]
    duration_ms: int


class NormalizationError(ValueError):
    """The ASR output could not be made into a valid token stream."""


def _offset_sentence(sentence: AsrSentence, offset_ms: int) -> AsrSentence:
    from animatter.providers.asr.base import AsrWord

    return AsrSentence(
        start_ms=sentence.start_ms + offset_ms,
        end_ms=sentence.end_ms + offset_ms,
        text=sentence.text,
        speaker_id=sentence.speaker_id,
        words=[
            AsrWord(
                text=word.text,
                start_ms=word.start_ms + offset_ms,
                end_ms=word.end_ms + offset_ms,
                punctuation=word.punctuation,
            )
            for word in sentence.words
        ],
    )


def normalize_channel(channel: AsrChannel, *, offset_ms: int = 0) -> NormalizedTranscript:
    """Convert one channel's sentences into tokens plus sentence ranges."""
    sentences = [_offset_sentence(sentence, offset_ms) for sentence in channel.sentences]

    tokens: list[TokenSpan] = []
    ranges: list[tuple[int, int, int | None]] = []

    for sentence in sentences:
        start_idx = len(tokens)
        tokens.extend(expand_words(_as_words(sentence), start_idx=start_idx))
        if len(tokens) == start_idx:
            # A sentence with no usable words (punctuation only) is dropped
            # rather than emitting an empty range, which would break INV-3.
            continue
        ranges.append((start_idx, len(tokens), sentence.speaker_id))

    if not tokens:
        raise NormalizationError("识别结果里没有任何可用的字词")

    spans = build_from_asr(ranges, len(tokens))
    validate(spans, len(tokens))

    predicted_end = max(token.end_ms for token in tokens)
    return NormalizedTranscript(
        tokens=tokens,
        sentences=spans,
        duration_ms=max(predicted_end, 0),
    )


def _as_words(sentence: AsrSentence):  # type: ignore[no-untyped-def]
    from animatter.core.tokens import WordSpan

    return [
        WordSpan(text=word.text, start_ms=word.start_ms, end_ms=word.end_ms, punctuation=word.punctuation)
        for word in sentence.words
    ]


def merge_segments(parts: list[NormalizedTranscript]) -> NormalizedTranscript:
    """Concatenate already-offset segments into one transcript.

    Sentences that straddle a seam are *not* rewritten here; the editor exposes
    seam handling as an explicit action so the user can see what was merged.
    This function only guarantees index continuity and INV-1..3.
    """
    if not parts:
        raise NormalizationError("没有可拼接的分段")
    if len(parts) == 1:
        return parts[0]

    tokens: list[TokenSpan] = []
    ranges: list[tuple[int, int, int | None]] = []

    for part in parts:
        shift = len(tokens)
        for token in part.tokens:
            tokens.append(
                TokenSpan(
                    idx=len(tokens),
                    start_ms=token.start_ms,
                    end_ms=token.end_ms,
                    text=token.text,
                    kind=token.kind,
                    src_word_idx=token.src_word_idx,
                    punc_after=token.punc_after,
                    derived=token.derived,
                )
            )
        for span in part.sentences:
            ranges.append((span.token_start + shift, span.token_end + shift, span.speaker_id))

    spans = build_from_asr(ranges, len(tokens))
    validate(spans, len(tokens))
    return NormalizedTranscript(
        tokens=tokens,
        sentences=spans,
        duration_ms=max((token.end_ms for token in tokens), default=0),
    )


def normalize(result: AsrResult, *, offset_ms: int = 0, channel: int = 0) -> NormalizedTranscript:
    """Pick one channel out of a multi-channel result and normalise it."""
    if not result.channels:
        raise NormalizationError("识别结果为空（channels 字段缺失）")
    chosen = next((c for c in result.channels if c.channel_id == channel), result.channels[0])
    return normalize_channel(chosen, offset_ms=offset_ms)
