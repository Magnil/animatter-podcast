"""Word → token expansion.

This is the module the rest of the editor stands on, and the one place where a
wrong assumption silently corrupts every downstream timestamp.

The ASR returns **word-level** spans. Chinese "words" are 2-4 characters with
one shared start/end, so per-character times do not exist and must be
interpolated. Every token produced here therefore carries ``derived=True``
unless the whole word is a single character (where the word time *is* the char
time). AGENT.md §6.3.

Invariants this module must preserve:

* ``idx`` is dense and ascending from 0.
* ``start_ms <= end_ms`` for every token, even when the ASR reported an
  inverted or zero-length word.
* Tokens do not overlap and are non-decreasing in time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: CJK ideographs, kana, and full-width forms — split these per character.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
#: Characters that terminate a clause; used only for the derived flag, never for splitting.
_PUNCT = set("，。！？；：、,.!?;:\"'）)》」』】…—-")


@dataclass(frozen=True, slots=True)
class WordSpan:
    """One ASR word with its shared timestamp."""

    text: str
    start_ms: int
    end_ms: int
    punctuation: str = ""


@dataclass(frozen=True, slots=True)
class TokenSpan:
    """One editable unit on the timeline."""

    idx: int
    start_ms: int
    end_ms: int
    text: str
    kind: str  # 'char' | 'word'
    src_word_idx: int
    punc_after: str
    derived: bool


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _split_units(text: str) -> list[str]:
    """Split one ASR word into the units a subtitle editor can select.

    CJK text is split per character; Latin text stays whole (splitting an
    English word in half would break both timing and wrapping).
    """
    stripped = text.strip()
    if not stripped:
        return []
    if _is_cjk(stripped):
        # Keep any interior ASCII run (e.g. "AI" in "AI技术") as one unit so the
        # editor never offers to cut "AI" into "A" + "I".
        units: list[str] = []
        buffer: list[str] = []
        for ch in stripped:
            if _CJK_RE.match(ch):
                if buffer:
                    units.append("".join(buffer))
                    buffer = []
                units.append(ch)
            elif ch.isspace():
                if buffer:
                    units.append("".join(buffer))
                    buffer = []
            else:
                buffer.append(ch)
        if buffer:
            units.append("".join(buffer))
        return units
    return [stripped]


def _interpolate(word: WordSpan, units: list[str]) -> list[tuple[int, int]]:
    """Distribute the word's duration across its units by character count.

    Degenerate inputs are clamped rather than rejected: a zero-length word still
    yields a zero-length token, and an inverted span is normalised so the
    timeline stays monotonic.
    """
    start, end = word.start_ms, word.end_ms
    if end < start:
        start, end = end, start
    total_chars = sum(len(unit) for unit in units)
    if total_chars == 0:
        return [(start, start)] * len(units)
    if end == start:
        return [(start, start)] * len(units)

    spans: list[tuple[int, int]] = []
    consumed = 0
    for unit in units:
        unit_start = start + round((end - start) * consumed / total_chars)
        consumed += len(unit)
        unit_end = start + round((end - start) * consumed / total_chars)
        spans.append((unit_start, max(unit_start, unit_end)))
    return spans


def expand_words(words: list[WordSpan], *, start_idx: int = 0) -> list[TokenSpan]:
    """Expand ASR words into the editable token stream.

    Punctuation attached to a word becomes ``punc_after`` on that word's last
    token instead of a token of its own — a punctuation mark has no duration and
    giving it one would make "delete this comma" a timeline operation.
    """
    tokens: list[TokenSpan] = []
    idx = start_idx

    for word_idx, word in enumerate(words):
        units = _split_units(word.text)
        if not units:
            # Whitespace-only word: attach its punctuation to the previous token.
            if word.punctuation and tokens:
                previous = tokens[-1]
                tokens[-1] = TokenSpan(
                    idx=previous.idx,
                    start_ms=previous.start_ms,
                    end_ms=previous.end_ms,
                    text=previous.text,
                    kind=previous.kind,
                    src_word_idx=previous.src_word_idx,
                    punc_after=previous.punc_after + word.punctuation,
                    derived=previous.derived,
                )
            continue

        spans = _interpolate(word, units)
        last = len(units) - 1
        for position, (unit, (unit_start, unit_end)) in enumerate(zip(units, spans, strict=True)):
            tokens.append(
                TokenSpan(
                    idx=idx,
                    start_ms=unit_start,
                    end_ms=unit_end,
                    text=unit,
                    kind="char" if _is_cjk(unit) or len(unit) == 1 else "word",
                    src_word_idx=word_idx,
                    punc_after=word.punctuation if position == last else "",
                    derived=len(units) > 1,
                )
            )
            idx += 1

    return tokens


def looks_like_sentence_end(text: str) -> bool:
    """Heuristic used when stitching segmentation seams (AGENT.md §6.2)."""
    return bool(text) and text[-1] in "。！？.!?"
