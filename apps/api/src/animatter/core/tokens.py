"""Word → token expansion.

This is the module the rest of the editor stands on, and the one place where a
wrong assumption silently corrupts every downstream timestamp.

The ASR returns **word-level** spans, and since 2026-11-06 the editor works at
exactly that granularity: **one token = one ASR word**. AGENTS.md §6.3.

Why word-level rather than character-level
------------------------------------------
Per-character times do not exist in the ASR output; Chinese "words" are 2-4
characters sharing one start/end, so character times were *interpolated*. That
interpolation bought a finer selection grid but cost a real invariant: editing a
span of different length redistributed the window and moved interior boundaries,
so a correction could nudge the very cut point the user had been listening for.

At word level a replacement is always 1 token → 1 token: the start and end are
both pinned by real ASR observations, there are no interior boundaries to move,
and the replacement text can be any length. The grid is coarser, which is the
honest price of never lying about timing.

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
    #: ``True`` once a human rewrote this token. Defaulted so every constructor
    #: that predates the flag keeps working and means "untouched".
    revised: bool = False
    #: ``True`` once a human hid this word from the *text* while leaving its
    #: audio in place. Defaulted for the same reason as ``revised``.
    hidden: bool = False


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _split_units(text: str) -> list[str]:
    """Split one ASR word into editable tokens.

    One word stays one token. This is deliberately **not** a per-character
    split: see the module docstring. Interior whitespace is trimmed because a
    token's text is what the editor renders inside a word cell.
    """
    stripped = text.strip()
    if not stripped:
        return []
    return [stripped]


def _interpolate(word: WordSpan, units: list[str]) -> list[tuple[int, int]]:
    """Distribute the word's duration across its units.

    With one unit per word this is the identity, but the function stays general:
    an imported SRT can still hand us a multi-unit "word", and the degenerate
    cases below (inverted or zero-length spans) must never produce a token whose
    start is after its end.
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
                    revised=previous.revised,
                    hidden=previous.hidden,
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
                    # At word level a token is a word even when it happens to be
                    # one character; "kind" describes the editing unit, not the
                    # script. Only an imported multi-unit split is still "char".
                    kind="word" if len(units) == 1 else "char",
                    src_word_idx=word_idx,
                    punc_after=word.punctuation if position == last else "",
                    # Word-level tokens carry the ASR's own timing, so they are
                    # observed rather than derived — the flag now only marks
                    # tokens whose times were interpolated (imported text).
                    derived=len(units) > 1,
                )
            )
            idx += 1

    return tokens


def looks_like_sentence_end(text: str) -> bool:
    """Heuristic used when stitching segmentation seams (AGENTS.md §6.2)."""
    return bool(text) and text[-1] in "。！？.!?"
