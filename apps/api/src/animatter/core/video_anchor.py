"""The sentence-boundary grid the video timeline snaps to (AGENTS.md §19.3).

Why this is a pure module
-------------------------
The grid decides where a dragged block lands and, after every audio edit, whether
an existing block boundary is still valid. Both are arithmetic over millisecond
integers, and both have sharp edges that are cheap to test here and expensive to
find in the UI:

* the grid is **not** uniformly spaced — on a real 583-sentence episode it runs
  from 160 ms to 37.8 s per tile, with 19 tiles under 500 ms;
* two sentence starts can be adjacent with **no silence between them** (13 of 582
  gaps were under 20 ms), so "snap to the nearest boundary" can move a cut by
  nothing at all;
* a boundary can disappear (its sentence was split or merged, or its audio was
  deleted), and every block edge sitting on it has to go somewhere that still
  makes sense.

The grid carries two extra points beyond the sentence starts: ``0`` and the media
duration. Real episodes have a lead-in before the first token (measured 240 ms to
5.1 s) and a tail after the last one (576 ms to 2.1 s); without those two points a
block could never start at the very beginning or end at the very end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: A boundary closer than this to the dragged position wins immediately; beyond
#: it the drag is free (the user is dropping into the middle of a sentence).
DEFAULT_SNAP_RADIUS_MS: Final[int] = 120

#: Boundaries closer together than this are shown as **one** snap target. The data
#: still has both, but a 160 ms tile is not a useful place to land a 3 s block and
#: offering it makes a drag feel jumpy.
MIN_SNAP_SPACING_MS: Final[int] = 500


def boundary_grid(sentence_starts: list[int], *, duration_ms: int) -> tuple[int, ...]:
    """Build the sorted, de-duplicated grid from sentence start instants.

    ``0`` and ``duration_ms`` are always present and always kept — they are the
    only way to place a block that starts before the first word or ends after the
    last one.
    """
    points = {0, max(0, duration_ms)}
    points.update(value for value in sentence_starts if 0 <= value <= duration_ms)
    return tuple(sorted(points))


def snap_targets(grid: tuple[int, ...], *, min_spacing_ms: int = MIN_SNAP_SPACING_MS) -> tuple[int, ...]:
    """Collapse clusters of nearby boundaries into single drag targets.

    The first boundary of a cluster wins. Keeping the *first* rather than the
    median is deliberate: a cluster is a burst of very short sentences, and
    landing on its start keeps the block covering all of them, which is what a
    human aiming at "that part" means. Aiming at the middle would cut a word in
    half in the middle of the burst.
    """
    if not grid:
        return ()
    if min_spacing_ms <= 0:
        return grid

    kept: list[int] = [grid[0]]
    for point in grid[1:]:
        if point - kept[-1] >= min_spacing_ms:
            kept.append(point)
    # The media end must survive the collapse: it is the only place a block can
    # end at the very end of the episode.
    if kept[-1] != grid[-1]:
        if len(kept) > 1 and grid[-1] - kept[-2] < min_spacing_ms:
            kept[-1] = grid[-1]
        else:
            kept.append(grid[-1])
    return tuple(kept)


def snap(value_ms: int, grid: tuple[int, ...], *, radius_ms: int = DEFAULT_SNAP_RADIUS_MS) -> int:
    """Move ``value_ms`` to the nearest boundary within ``radius_ms``, else leave it.

    Ties go to the **earlier** boundary. A tie means the drag is exactly between
    two sentence starts, and keeping more audio (starting earlier) is the safer
    mistake: the user can always trim forward, but audio that was cut is gone.
    """
    if not grid or radius_ms <= 0:
        return value_ms

    best = value_ms
    best_distance = radius_ms + 1
    for point in grid:
        distance = abs(point - value_ms)
        # Ascending grid plus strict ``<`` means a tie keeps the earlier point.
        if distance < best_distance:
            best = point
            best_distance = distance
    return best if best_distance <= radius_ms else value_ms


@dataclass(frozen=True)
class Interval:
    """A half-open ``[start_ms, end_ms)`` block on the original timeline."""

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def valid(self) -> bool:
        return self.end_ms > self.start_ms


def renormalize(
    blocks: list[Interval],
    grid: tuple[int, ...],
    *,
    duration_ms: int,
) -> list[Interval]:
    """Re-seat every block edge on a surviving boundary.

    Called after any edit that can change the set of sentence starts. An edge that
    still sits on a boundary does not move — that is the whole point of storing
    absolute instants: the common case (deleting audio inside a block) must not
    touch the block at all.

    An edge whose boundary vanished moves to the nearest surviving one. When that
    would collapse the block (``end <= start``), the block is extended to the next
    boundary rather than dropped — dropping a row here would destroy the user's
    clip on behalf of an edit they can undo.
    """
    if not blocks:
        return []
    if not grid:
        grid = (0, max(0, duration_ms))

    moved: list[Interval] = []
    for block in blocks:
        start = nearest_boundary(block.start_ms, grid)
        end = nearest_boundary(block.end_ms, grid)
        if end <= start:
            later = [point for point in grid if point > start]
            end = later[0] if later else max(start + 1, duration_ms)
        moved.append(Interval(start_ms=start, end_ms=end))
    return moved


def nearest_boundary(value_ms: int, grid: tuple[int, ...]) -> int:
    """The grid point closest to ``value_ms``, with no distance limit.

    ``renormalize`` must be *total*: an edge that no longer sits on any boundary
    has to land somewhere deterministic, and "leave it where it is" would leave a
    block whose edge is a cut point that no longer exists.

    Ties go to the earlier boundary, same rule as :func:`snap` — keeping audio is
    the safer mistake.
    """
    best = grid[0]
    best_distance = abs(grid[0] - value_ms)
    for point in grid[1:]:
        distance = abs(point - value_ms)
        if distance < best_distance:
            best = point
            best_distance = distance
    return best


def boundaries_in_use(blocks: list[Interval]) -> set[int]:
    """Every instant a block currently starts or ends on."""
    used: set[int] = set()
    for block in blocks:
        used.add(block.start_ms)
        used.add(block.end_ms)
    return used


def orphaned_blocks(blocks: list[Interval], keeps: list[tuple[int, int]]) -> list[int]:
    """Indices of blocks with no surviving audio at all.

    This is the one case that genuinely breaks a block: the sentence it covers was
    deleted in full, so its rendered span is empty and ``map_range`` returns
    ``None``. Measured on real edits, this happens to 7.54 % of sentences in a
    1074-sentence project — rare enough to be a surprise, common enough that it
    needs an explicit path rather than a crash.
    """
    doomed: list[int] = []
    for index, block in enumerate(blocks):
        covered = sum(
            max(0, min(block.end_ms, keep_end) - max(block.start_ms, keep_start))
            for keep_start, keep_end in keeps
        )
        if covered <= 0:
            doomed.append(index)
    return doomed


def kept_ms(block: Interval, keeps: list[tuple[int, int]]) -> int:
    """How long this block survives in the rendered output.

    The block plays continuously for this long — it is **not** the same as the
    original length, and it must not be computed from the block edges alone.
    Snapping deletions to silence plus the 20 ms guard can move a cut off the
    boundary by up to 20 ms or a whole silence span, so the only correct answer
    comes from the compiled keep list.
    """
    return sum(
        max(0, min(block.end_ms, keep_end) - max(block.start_ms, keep_start))
        for keep_start, keep_end in keeps
    )
