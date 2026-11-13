"""Avatar lit/dim arithmetic: who is talking, and when (§19.5).

An avatar slot shows **lit** while its speaker is talking and **dim** otherwise. Both
halves matter: a slot that is lit all the time is indistinguishable from the PSD's static
poster, and a slot that is dim all the time hides a panel member who is in the room.

Why this is a pure module
------------------------
Everything here is interval arithmetic over millisecond integers, and every version of it
that is wrong produces a *plausible* film: an avatar that lags 200 ms behind the voice, one
that stays lit through the other person's question, or a track that ends a few frames
before the film does — which shows up as a frozen last frame rather than an error.

The three things that break it in practice, all measured on real projects:

* sentences **overlap** (a boundary edit can leave a 40 ms overlap), so "for each sentence
  emit a segment" produces a track longer than the film;
* the same speaker talks again a second later, and treating that as "still lit" is wrong
  for the *other* slots — they have to go dim in the gap;
* the film is shorter than the original timeline, and a range that was deleted must not
  resurrect as a lit interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Gaps shorter than this between two intervals of the same speaker are bridged. Measured
#: on real episodes, consecutive sentences from one speaker are separated by 200–600 ms of
#: breath; flickering the avatar off and on inside half a second looks like a glitch, not
#: like turn-taking.
BRIDGE_MS: Final[int] = 600


@dataclass(frozen=True)
class Interval:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def merge(
    intervals: list[Interval],
    *,
    total_ms: int,
    bridge_ms: int = BRIDGE_MS,
) -> list[Interval]:
    """Sort, clip to the film, and join intervals that are near-touching.

    ``bridge_ms=0`` keeps every gap, which is what a test asserting exact turn boundaries
    wants; the default bridges breaths so the avatar does not strobe.
    """
    if total_ms <= 0:
        return []
    clipped = [
        Interval(max(0, item.start_ms), min(total_ms, item.end_ms))
        for item in intervals
        if min(total_ms, item.end_ms) > max(0, item.start_ms)
    ]
    if not clipped:
        return []
    clipped.sort(key=lambda item: (item.start_ms, item.end_ms))

    merged: list[Interval] = [clipped[0]]
    for item in clipped[1:]:
        last = merged[-1]
        if item.start_ms <= last.end_ms + bridge_ms:
            # Overlapping or close enough: extend. `max` on the end, because an interval
            # fully inside the last one must not shrink it.
            merged[-1] = Interval(last.start_ms, max(last.end_ms, item.end_ms))
        else:
            merged.append(item)
    return merged


def invert(lit: list[Interval], *, total_ms: int) -> list[Interval]:
    """The stretches where a slot is **not** lit — i.e. the dim stretches."""
    gaps: list[Interval] = []
    cursor = 0
    for item in lit:
        if item.start_ms > cursor:
            gaps.append(Interval(cursor, item.start_ms))
        cursor = max(cursor, item.end_ms)
    if cursor < total_ms:
        gaps.append(Interval(cursor, total_ms))
    return gaps


@dataclass(frozen=True)
class TrackPiece:
    """One stretch of an avatar track: lit or dim, for how long."""

    lit: bool
    duration_ms: int


def track_pieces(lit: list[Interval], *, total_ms: int) -> list[TrackPiece]:
    """Turn "when this speaker talks" into a lit/dim sequence tiling the whole film.

    The result **must** total ``total_ms`` exactly: an avatar track that is short ends the
    overlay early and freezes the frame, and one that is long pushes the film's duration.
    The last piece is therefore anchored to the film's end rather than to the last lit
    interval.
    """
    if total_ms <= 0:
        return []
    pieces: list[TrackPiece] = []
    cursor = 0
    for item in lit:
        if item.start_ms > cursor:
            pieces.append(TrackPiece(lit=False, duration_ms=item.start_ms - cursor))
        pieces.append(TrackPiece(lit=True, duration_ms=item.end_ms - item.start_ms))
        cursor = item.end_ms
    if cursor < total_ms:
        pieces.append(TrackPiece(lit=False, duration_ms=total_ms - cursor))
    return [piece for piece in pieces if piece.duration_ms > 0]


def pieces_in(lit: list[Interval], *, start_ms: int, end_ms: int) -> list[TrackPiece]:
    """The lit/dim sequence for one **slice** of the film, rebased to start at 0.

    Used by the chunked export (§19.28). A chunk cannot simply seek the whole-film track: the
    track is a sparse VFR clip (298 frames over 61 minutes), and cutting it with
    ``-ss <start> -t <seconds>`` makes ffmpeg drop frames and then end the stream early — with
    ``eof_action=pass`` on the overlay, "ends early" means the avatar silently stops being drawn
    for the rest of the chunk. Measured on the 费那奇MV film: a 133 s chunk declaring the track
    with ``-t`` got 7 of 298 frames (last at 67 s) while the avatar stayed on screen only to
    67 s.

    Building a fresh short track per chunk is the fix: it is a handful of stills, the concat
    demuxer is exact, and there is no seek to get wrong. ``start_ms`` mid-interval keeps the
    state that was current at that instant, so a chunk that begins while someone is talking
    starts lit rather than dipping to dim for one frame.
    """
    if end_ms <= start_ms:
        return []
    out: list[TrackPiece] = []
    cursor = 0
    for piece in track_pieces(lit, total_ms=end_ms):
        piece_end = cursor + piece.duration_ms
        if piece_end <= start_ms:
            cursor = piece_end
            continue
        begin = max(cursor, start_ms)
        end = min(piece_end, end_ms)
        if end > begin:
            out.append(TrackPiece(lit=piece.lit, duration_ms=end - begin))
        cursor = piece_end
    return out


def lit_at(lit: list[Interval], at_ms: int) -> bool:
    """Whether the slot is lit at one instant — used by the still preview.

    Half-open, like every other interval in this codebase: an instant equal to an
    interval's ``end_ms`` belongs to the next piece, not this one.
    """
    return any(item.start_ms <= at_ms < item.end_ms for item in lit)


def any_lit(lit: list[Interval], *, total_ms: int) -> bool:
    """Whether a slot ever lights up at all.

    A slot bound to a speaker who never speaks in the film stays dim, and the renderer can
    skip building a track for it — one constant still is enough. Worth checking because on
    a two-speaker / four-slot template half the slots are exactly that.
    """
    del total_ms  # kept for symmetry with the other helpers; the answer needs only `lit`
    return bool(lit)
