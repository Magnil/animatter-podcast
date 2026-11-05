"""Text repair that cannot move the timeline.

The hard rule of this module (AGENT.md §6.3): a correction may change *what is
written*, never *when it happened*. Character-level timestamps are interpolated
from ASR word spans, so letting a model rewrite text freely would silently
destroy every downstream timestamp — subtitles would drift and cuts would land
in the wrong place.

Therefore every correction is applied **inside an existing token range**: the
range's outer boundaries (``start_ms`` of the first token, ``end_ms`` of the last)
are preserved exactly, and the new text is redistributed proportionally over the
same tokens.

Two shapes are supported:

* same-length replacement ("剧集" → "句级"): timings are reused verbatim;
* different-length replacement ("人工智能" → "AI"): the new tokens inherit the
  old range and are interpolated *within it*.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from animatter.core.tokens import TokenSpan, _split_units


class RealignError(ValueError):
    """A correction could not be applied without violating the timeline."""


@dataclass(frozen=True, slots=True)
class TokenRange:
    """A half-open ``[start, end)`` index range over the token stream."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def locate_text(
    tokens: list[TokenSpan], needle: str, *, start: int = 0, end: int | None = None
) -> list[TokenRange]:
    """Find every token range whose concatenated text equals ``needle``.

    Matching ignores punctuation that is attached to tokens via ``punc_after``,
    because the model quotes clause text without the trailing comma and a
    mismatch there would make every candidate look unapplicable.
    """
    if not needle:
        return []
    limit = len(tokens) if end is None else min(end, len(tokens))

    matches: list[TokenRange] = []
    for first in range(max(start, 0), limit):
        buffer: list[str] = []
        for last in range(first, limit):
            buffer.append(tokens[last].text)
            candidate = "".join(buffer)
            if candidate == needle:
                matches.append(TokenRange(first, last + 1))
                break
            if not needle.startswith(candidate):
                break
    return matches


def locate_sentence(
    tokens: list[TokenSpan], sentence_start: int, sentence_end: int, needle: str
) -> TokenRange | None:
    """Locate text inside one sentence; prefers the match nearest the start."""
    matches = locate_text(tokens, needle, start=sentence_start, end=sentence_end)
    return matches[0] if matches else None


def _reinterpolate(tokens: list[TokenSpan], span: TokenRange, new_text: str) -> list[TokenSpan]:
    """Replace ``tokens[span]`` with new tokens covering the same time window."""
    if span.length <= 0:
        raise RealignError("空的替换区间")

    window_start = tokens[span.start].start_ms
    window_end = tokens[span.end - 1].end_ms
    if window_end < window_start:
        raise RealignError("原区间时间倒挂，拒绝改写")

    units = _split_units(new_text)
    if not units:
        raise RealignError("替换文本为空，应使用删除操作而非改写")

    # Punctuation and provenance carry over from the original tail.
    trailing_punct = tokens[span.end - 1].punc_after
    src_word_idx = tokens[span.start].src_word_idx

    total_chars = sum(len(unit) for unit in units) or 1
    duration = window_end - window_start

    rebuilt: list[TokenSpan] = []
    consumed = 0
    for position, unit in enumerate(units):
        if duration == 0:
            unit_start = unit_end = window_start
        else:
            unit_start = window_start + round(duration * consumed / total_chars)
            consumed += len(unit)
            unit_end = window_start + round(duration * consumed / total_chars)
            unit_start = min(unit_start, window_end)
            unit_end = max(unit_end, unit_start)
        rebuilt.append(
            TokenSpan(
                idx=0,  # renumbered by the caller
                start_ms=unit_start,
                end_ms=unit_end,
                text=unit,
                kind="char" if len(unit) == 1 else "word",
                src_word_idx=src_word_idx,
                punc_after=trailing_punct if position == len(units) - 1 else "",
                # Replacement timing is derived, never observed.
                derived=True,
                # This text is now what a person wrote, not what the ASR heard.
                revised=True,
            )
        )

    # Same number of units: keep each unit's *original* boundary. Re-deriving the
    # interior boundaries would move them even when nothing about the audio
    # changed — "句级"→"句集" must not shift 14960 to 15000, because the pause
    # the editor heard is still where it was. Only the text differs.
    if len(units) == span.length:
        rebuilt = [
            replace(token, start_ms=source.start_ms, end_ms=source.end_ms)
            for token, source in zip(rebuilt, tokens[span.start : span.end], strict=True)
        ]

    # The last token must land exactly on the window end so the following
    # tokens keep their absolute positions.
    last = rebuilt[-1]
    rebuilt[-1] = replace(last, end_ms=max(last.start_ms, window_end))
    if rebuilt[0].start_ms != window_start:
        rebuilt[0] = replace(rebuilt[0], start_ms=window_start)
    return rebuilt


@dataclass(frozen=True, slots=True)
class RealignResult:
    tokens: list[TokenSpan]
    token_delta: int
    """How many tokens were added (+) or removed (−)."""


def apply_replacement(tokens: list[TokenSpan], span: TokenRange, new_text: str) -> RealignResult:
    """Return a new token list with ``span`` rewritten to ``new_text``."""
    if span.end > len(tokens):
        raise RealignError(f"区间 {span.start}–{span.end} 超出 token 总数 {len(tokens)}")

    outcome = tokens[: span.start] + _reinterpolate(tokens, span, new_text) + tokens[span.end :]

    # Invariant: the timeline outside the edited window must be untouched.
    before_start = tokens[span.start].start_ms
    after_start = outcome[span.start].start_ms
    if before_start != after_start:
        raise RealignError("改写改变了区间起点时间")

    new_count = len(_split_units(new_text))
    before_end = tokens[span.end - 1].end_ms
    after_end = outcome[span.start + new_count - 1].end_ms
    if before_end != after_end:
        raise RealignError("改写改变了区间终点时间")

    # Invariant: a same-length replacement must not move *any* boundary inside the
    # window either. Checking only the outer edges let "句级"→"句集" silently
    # shift the midpoint from 14960 to 15000, which the user hears as their cut
    # point drifting.
    if new_count == span.length:
        for offset, source in enumerate(tokens[span.start : span.end]):
            written = outcome[span.start + offset]
            if (written.start_ms, written.end_ms) != (source.start_ms, source.end_ms):
                raise RealignError(
                    f"改写字数相同却移动了第 {offset} 个 token 的时间"
                    f"（{source.start_ms}–{source.end_ms} → {written.start_ms}–{written.end_ms}）"
                )

    renumbered = [replace(token, idx=position) for position, token in enumerate(outcome)]
    assert_monotonic(renumbered)
    return RealignResult(tokens=renumbered, token_delta=len(renumbered) - len(tokens))


def assert_monotonic(tokens: list[TokenSpan]) -> None:
    """Reject any token stream whose timeline goes backwards or overlaps."""
    previous_end = -1
    for token in tokens:
        if token.start_ms < previous_end:
            raise RealignError(
                f"token {token.idx} 起点 {token.start_ms}ms 早于前一个 token 的结束 {previous_end}ms"
            )
        if token.end_ms < token.start_ms:
            raise RealignError(f"token {token.idx} 时间倒挂：{token.start_ms}ms → {token.end_ms}ms")
        previous_end = token.end_ms


def shift_ranges_after(
    sentences: list[tuple[int, int]],
    *,
    at_token: int,
    delta: int,
) -> list[tuple[int, int]]:
    """Adjust sentence ranges after a token-count change at ``at_token``.

    Three cases matter, and getting the middle one wrong silently shifts every
    following subtitle:

    * sentence ends at or before the edit → untouched;
    * sentence **starts** at the edit point → it contains the edit, so only its
      end moves (its start stays put);
    * sentence starts after the edit point → both ends move by ``delta``.
    """
    if delta == 0:
        return list(sentences)

    adjusted: list[tuple[int, int]] = []
    for start, end in sentences:
        if end <= at_token:
            adjusted.append((start, end))
        elif start <= at_token < end:
            # Contains the edit point: only the tail moves.
            adjusted.append((start, end + delta))
        else:
            adjusted.append((start + delta, end + delta))
    return adjusted


#: A boundary drag must leave the token audible; zero-length tokens break every
#: downstream consumer (subtitle timing, cut points).
MIN_TOKEN_MS = 10


@dataclass(frozen=True, slots=True)
class EdgeAdjustment:
    """Outcome of moving one token edge."""

    applied_ms: int
    clamped: bool
    reason: str = ""


def clamp_edge(new_ms: int, low: int, high: int) -> EdgeAdjustment:
    """Clamp a dragged boundary into ``[low, high]``.

    ``low``/``high`` come from the adjacent tokens, so a drag can never cause an
    overlap or reorder the stream — it can only consume silence between words.
    """
    if high < low:
        low, high = high, low
    if new_ms < low:
        return EdgeAdjustment(applied_ms=low, clamped=True, reason=f"不能早于 {low}ms（会与相邻字重叠）")
    if new_ms > high:
        return EdgeAdjustment(applied_ms=high, clamped=True, reason=f"不能晚于 {high}ms（会与相邻字重叠）")
    return EdgeAdjustment(applied_ms=new_ms, clamped=False)


def adjust_token_edge(
    tokens: list[TokenSpan], index: int, edge: str, new_ms: int
) -> tuple[TokenSpan, EdgeAdjustment]:
    """Move one token's ``start`` or ``end`` to ``new_ms``, clamped to neighbours.

    Only the addressed token changes. Every other token keeps its exact timing,
    which is the whole point: a fix to a sentence boundary must not nudge the
    words inside it.
    """
    if edge not in {"start", "end"}:
        raise RealignError(f"未知的边界方向 {edge!r}")
    if not 0 <= index < len(tokens):
        raise RealignError(f"token {index} 超出范围")

    token = tokens[index]

    if edge == "start":
        # Cannot start before the previous token ends, nor swallow this token.
        low = tokens[index - 1].end_ms if index > 0 else 0
        high = token.end_ms - MIN_TOKEN_MS
        outcome = clamp_edge(new_ms, low, high)
        if outcome.applied_ms == token.start_ms:
            return token, outcome
        return replace(token, start_ms=outcome.applied_ms), outcome

    low = token.start_ms + MIN_TOKEN_MS
    high = tokens[index + 1].start_ms if index + 1 < len(tokens) else max(low, token.end_ms)
    outcome = clamp_edge(new_ms, low, high)
    if outcome.applied_ms == token.end_ms:
        return token, outcome
    return replace(token, end_ms=outcome.applied_ms), outcome
