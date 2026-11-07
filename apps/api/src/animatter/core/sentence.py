"""Sentence range algebra.

Sentences are stored as half-open token ranges ``[token_start, token_end)``
rather than as parent pointers on tokens, because re-drawing a boundary is a
range operation and parent pointers would leave dangling references behind
(AGENT.md §6.4).

Every function here returns a *new* list; nothing mutates in place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class SentenceSpan:
    ord: int
    token_start: int
    token_end: int
    speaker_id: int | None = None
    origin: str = "asr"

    @property
    def length(self) -> int:
        return self.token_end - self.token_start


class InvalidBoundaryError(ValueError):
    """Raised when an edit would violate an invariant (AGENT.md INV-1..4)."""


def _merged_speaker(spans: list[SentenceSpan], first: int, last: int) -> int | None:
    """Speaker for the sentence that results from merging ``spans[first..last]``.

    Refuses to merge across two *different* annotated speakers: a sentence
    carries one speaker, so collapsing a question and its answer into one row
    would have to throw one of the two away, and the transcript would then
    attribute the answer to the person who asked. Unannotated neighbours merge
    freely — there is nothing to contradict.

    When exactly one side is annotated, that annotation survives. Keeping
    "whatever the first sentence had" would silently drop a label the user had
    just set on the second one.
    """
    annotated = {span.speaker_id for span in spans[first : last + 1] if span.speaker_id is not None}
    if len(annotated) > 1:
        raise InvalidBoundaryError("SPEAKER-1: 这两句属于不同说话人，合并会把其中一人的发言算到另一人头上")
    return next(iter(annotated)) if annotated else None


def _guard_cross_speaker(host: SentenceSpan, donor: SentenceSpan) -> None:
    """Refuse to move ``donor``'s words under ``host``'s speaker label.

    The whole-sentence case is a merge and is guarded there. This is the partial
    case: 「并入上句」 on half of the *next* sentence moves those words into the
    previous sentence, whose speaker label then covers words that person did not
    say. Same misalignment, reached by a different button.
    """
    if host.speaker_id is not None and donor.speaker_id is not None and host.speaker_id != donor.speaker_id:
        raise InvalidBoundaryError("SPEAKER-1: 这两句属于不同说话人，把词移过去会把一人的发言算到另一人头上")


def renumber(spans: list[SentenceSpan]) -> list[SentenceSpan]:
    """Reassign ``ord`` from 0 so display order is always positional."""
    return [replace(span, ord=position) for position, span in enumerate(spans)]


def validate(spans: list[SentenceSpan], token_count: int) -> None:
    """Assert INV-1..3. Raises :class:`InvalidBoundaryError` with the broken rule."""
    if not spans:
        raise InvalidBoundaryError("INV-2: no sentences")

    ordered = sorted(spans, key=lambda s: s.token_start)
    if ordered[0].token_start != 0:
        raise InvalidBoundaryError(f"INV-2: first sentence starts at {ordered[0].token_start}, expected 0")

    expected = 0
    for span in ordered:
        if span.token_start != expected:
            gap = span.token_start - expected
            kind = "overlap" if gap < 0 else "gap"
            raise InvalidBoundaryError(f"INV-1: {kind} of {abs(gap)} tokens before token {span.token_start}")
        if span.token_end <= span.token_start:
            raise InvalidBoundaryError(f"INV-3: empty sentence at token {span.token_start}")
        expected = span.token_end

    if expected != token_count:
        raise InvalidBoundaryError(f"INV-2: last sentence ends at {expected}, expected {token_count}")


def build_from_asr(asr_sentences: list[tuple[int, int, int | None]], token_count: int) -> list[SentenceSpan]:
    """Build initial spans from ASR sentence boundaries.

    ``asr_sentences`` is ``(token_start, token_end, speaker_id)`` per sentence, in
    order. Gaps created by dropped punctuation tokens are closed by extending the
    previous sentence, so INV-1 holds without inventing phantom sentences.
    """
    spans: list[SentenceSpan] = []
    cursor = 0
    for start, end, speaker in asr_sentences:
        start = max(start, cursor)
        end = min(max(end, start), token_count)
        if end <= start:
            continue
        if start > cursor:
            # Close the gap by growing the previous sentence; if there is none,
            # extend this one backwards.
            if spans:
                spans[-1] = replace(spans[-1], token_end=start)
            else:
                start = cursor
        spans.append(SentenceSpan(ord=len(spans), token_start=start, token_end=end, speaker_id=speaker))
        cursor = end

    if not spans:
        if token_count == 0:
            return []
        return [SentenceSpan(ord=0, token_start=0, token_end=token_count)]

    if cursor < token_count:
        spans[-1] = replace(spans[-1], token_end=token_count)

    return renumber(spans)


def split_at(spans: list[SentenceSpan], token_index: int) -> list[SentenceSpan]:
    """Break the sentence containing ``token_index`` into two at that index."""
    if token_index <= 0:
        raise InvalidBoundaryError("cannot split before the first token")

    for position, span in enumerate(spans):
        if span.token_start < token_index < span.token_end:
            left = replace(span, token_end=token_index)
            right = replace(span, token_start=token_index, origin="manual_split")
            return renumber([*spans[:position], left, right, *spans[position + 1 :]])
        if token_index == span.token_start:
            raise InvalidBoundaryError(f"token {token_index} already starts a sentence")
        if token_index == span.token_end:
            raise InvalidBoundaryError(f"token {token_index} does not fall inside a sentence")

    raise InvalidBoundaryError(f"token {token_index} is outside the stream")


def merge_range(spans: list[SentenceSpan], from_token: int, to_token: int) -> list[SentenceSpan]:
    """Merge every sentence touched by ``[from_token, to_token)`` into one."""
    if to_token <= from_token:
        raise InvalidBoundaryError("merge range is empty")

    touched = [
        position
        for position, span in enumerate(spans)
        if span.token_start < to_token and span.token_end > from_token
    ]
    if not touched:
        raise InvalidBoundaryError("merge range touches no sentence")

    first, last = touched[0], touched[-1]
    merged = replace(
        spans[first],
        token_start=spans[first].token_start,
        token_end=spans[last].token_end,
        speaker_id=_merged_speaker(spans, first, last),
        origin="manual_merge",
    )
    return renumber([*spans[:first], merged, *spans[last + 1 :]])


def extend_into(
    spans: list[SentenceSpan], from_token: int, to_token: int, *, direction: str
) -> list[SentenceSpan]:
    """Move a token range into the previous or next sentence.

    ``direction='prev'`` grows the sentence above; ``'next'`` grows the one below.
    This implements the editor's 「并入上句 / 并入下句」 actions.

    Selecting a **whole sentence** and asking to join a neighbour is the common
    way users merge two sentences, so it degrades to a merge rather than an
    error: the user should not have to discover a different button for "merge
    this sentence into the one above".
    """
    if direction not in {"prev", "next"}:
        raise InvalidBoundaryError(f"unknown direction {direction!r}")
    if to_token <= from_token:
        raise InvalidBoundaryError("selection is empty")

    for position, span in enumerate(spans):
        if not (span.token_start <= from_token and to_token <= span.token_end):
            continue

        whole_sentence = from_token == span.token_start and to_token == span.token_end
        if whole_sentence:
            if direction == "prev":
                if position == 0:
                    raise InvalidBoundaryError("no previous sentence")
                return merge_range(spans, spans[position - 1].token_start, span.token_end)
            if position == len(spans) - 1:
                raise InvalidBoundaryError("no next sentence")
            return merge_range(spans, span.token_start, spans[position + 1].token_end)

        result = list(spans)
        if direction == "prev":
            if position == 0:
                raise InvalidBoundaryError("no previous sentence")
            _guard_cross_speaker(result[position - 1], span)
            moved = replace(span, token_start=to_token)
            grown = replace(result[position - 1], token_end=to_token)
            result[position - 1] = grown
            result[position] = moved
        else:
            if position == len(spans) - 1:
                raise InvalidBoundaryError("no next sentence")
            _guard_cross_speaker(result[position + 1], span)
            moved = replace(span, token_end=from_token)
            grown = replace(result[position + 1], token_start=from_token)
            result[position] = moved
            result[position + 1] = grown

        return renumber([s for s in result if s.length > 0])

    raise InvalidBoundaryError("selection spans more than one sentence")
