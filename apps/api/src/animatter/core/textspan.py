"""Multi-colour text: ``**...**`` marks the accent run (§19.14).

The PSD's own runs are the spec. Measured on the sample (run lengths + colours, per character):

* ``EP.23`` → ``EP.`` blue, ``23`` red  →  ``EP.**23**``
* the title → ``《罗小黑战记2》`` blue, ``专访`` red, ``六年磨一剑背后的`` blue, ``苦与乐`` red
  → ``《罗小黑战记2》**独家专访**六年磨一剑背后的**苦与乐**``

So the markup is a **pair** of asterisks around the accent, not a per-character map. A map would
have to survive the user retyping the text, and there is no sane answer to "what colour is the
character the user just typed". ``**`` is what every editor uses for the same job, and it
degrades honestly: an unpaired ``**`` is treated as literal text rather than swallowing the rest
of the line into red.

Why the accent colour is not per-slot
-------------------------------------
The design has exactly two colours and they are the same two everywhere — the brand blue and the
brand red/pink. Storing a colour per slot would let a user produce a title that is blue-and-green
while the footer is blue-and-red, which is not a thing the design supports. One accent, declared
once.

This module is pure: the renderer supplies the default colour and gets segments back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The design's two colours, measured from the PSD rather than sampled by eye:
#: ``EP.23`` is ``EP.`` in blue + ``23`` in red, and the title alternates the same pair.
BLUE: Final[str] = "#1947ba"
RED: Final[str] = "#f9485c"

#: Kept for callers that want "the accent" without knowing the base.
ACCENT: Final[str] = RED

#: The marker. Two asterisks, escaped by doubling: ``****`` is one literal ``**``.
MARK: Final[str] = "**"


@dataclass(frozen=True)
class Segment:
    """A stretch of one colour, in reading order."""

    text: str
    colour: str


def accent_for(base: str) -> str:
    """The *other* brand colour for a slot whose base is ``base``.

    Not a per-slot setting, deliberately: the design has exactly two colours and they are the
    same pair everywhere. What varies is which one a slot is made of — the episode number and
    the title are blue with red accents, the footer is all red — so the accent is whichever of
    the two the slot is **not** already. That keeps every default identical to the PSD while
    still letting ``**`` introduce the second colour anywhere.
    """
    return RED if base.strip().lower() == BLUE.lower() else BLUE


def parse(text: str, *, base: str) -> list[Segment]:
    """Split ``text`` into coloured segments, ``**`` toggling the accent colour.

    Newlines are preserved inside the segments so the caller can wrap and lay out exactly as it
    would for unmarked text — the marking is removed before measuring, otherwise ``**`` would
    occupy width and the title would wrap earlier than the design says.

    An unpaired marker loses its special meaning rather than being dropped: the fastest way to
    destroy trust in an editor is to have half the title silently change colour because one
    asterisk was forgotten.
    """
    if MARK not in text:
        return [Segment(text=text, colour=base)] if text else []

    if _unpaired(text):
        return [Segment(text=text, colour=base)] if text else []

    segments: list[Segment] = []
    accent = False
    accent_colour = accent_for(base)
    cursor = 0
    while cursor < len(text):
        if text.startswith(MARK, cursor):
            accent = not accent
            cursor += len(MARK)
            continue
        next_mark = text.find(MARK, cursor)
        end = len(text) if next_mark < 0 else next_mark
        chunk = text[cursor:end]
        if chunk:
            segments.append(
                Segment(text=chunk, colour=accent_colour if accent else base)
            )
        cursor = end

    return _merged(segments)


def plain(text: str) -> str:
    """``text`` with the markers removed — what the layout measures and wraps."""
    if _unpaired(text):
        return text
    return text.replace(MARK, "")


def is_marked(text: str, *, base: str = BLUE) -> bool:
    """Whether this text asks for **more than one colour**.

    Not "contains a marker": ``a****b`` doubles as an escaped pair and parses back to one run,
    and a caller that treated it as marked would go looking for a second colour that is not
    there.
    """
    return MARK in text and not _unpaired(text) and len(parse(text, base=base)) > 1


def _unpaired(text: str) -> bool:
    """An odd number of markers, counting ``****`` as an escaped pair."""
    return text.count(MARK) % 2 == 1


def _merged(segments: list[Segment]) -> list[Segment]:
    """Join neighbours of the same colour, so ``a****b`` is one run."""
    out: list[Segment] = []
    for segment in segments:
        if out and out[-1].colour == segment.colour:
            out[-1] = Segment(text=out[-1].text + segment.text, colour=segment.colour)
        else:
            out.append(segment)
    return out


def marked(colour: str) -> str:
    """``colour`` next to its accent, for tests and documentation."""
    return f"{colour} / {accent_for(colour)}"
