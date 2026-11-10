"""Sentence-boundary snapping and block bookkeeping (AGENTS.md §19.3).

Everything here is arithmetic over integer milliseconds, but the arithmetic is
what decides whether a dragged block lands where the user aimed and whether an
existing block survives an audio edit. The cases below are the ones measured on
real episodes, not invented:

* a 160 ms tile next to a 37.8 s one;
* two sentence starts 0 ms apart;
* a block whose audio was deleted in full (7.54 % of sentences in a 1074-sentence
  project);
* a deletion that crosses a block boundary without breaking it.
"""

from __future__ import annotations

import pytest

from animatter.core.video_anchor import (
    DEFAULT_SNAP_RADIUS_MS,
    Interval,
    boundaries_in_use,
    boundary_grid,
    kept_ms,
    nearest_boundary,
    orphaned_blocks,
    renormalize,
    snap,
    snap_targets,
)

# --- the grid -----------------------------------------------------------


def test_grid_always_contains_both_media_ends() -> None:
    """A real episode starts 240–5100 ms before the first word.

    Without the explicit 0 and duration points, a block could never start at the
    very beginning or end at the very end — and the first sentence of a 583-
    sentence project started at 5.1 s, so that gap is not hypothetical.
    """
    grid = boundary_grid([5100, 18780], duration_ms=3695928)
    assert grid[0] == 0
    assert grid[-1] == 3695928
    assert 5100 in grid


def test_grid_drops_points_outside_the_media_and_deduplicates() -> None:
    grid = boundary_grid([-5, 0, 1000, 1000, 5000], duration_ms=4000)
    assert grid == (0, 1000, 4000)


def test_grid_of_an_empty_transcript_is_just_the_ends() -> None:
    assert boundary_grid([], duration_ms=1000) == (0, 1000)


def test_snap_targets_collapse_a_burst_of_short_sentences() -> None:
    """19 real tiles were under 500 ms; they must not each be a drag target."""
    grid = (0, 160, 320, 480, 5000)
    targets = snap_targets(grid, min_spacing_ms=500)
    assert targets == (0, 5000)


def test_snap_targets_keep_the_first_of_a_cluster() -> None:
    # Landing on the cluster's start keeps the block covering all of it; landing
    # on the middle would cut a word in half inside the burst.
    assert snap_targets((1000, 1100, 1200, 4000), min_spacing_ms=500) == (1000, 4000)


def test_snap_targets_never_lose_the_media_end() -> None:
    """The last point must survive even when it sits inside the final cluster."""
    targets = snap_targets((0, 900, 1000), min_spacing_ms=500)
    assert targets[-1] == 1000


def test_snap_targets_are_the_grid_when_spacing_is_disabled() -> None:
    assert snap_targets((0, 100, 200), min_spacing_ms=0) == (0, 100, 200)


# --- snapping -----------------------------------------------------------


def test_snap_moves_within_the_radius() -> None:
    assert snap(1080, (0, 1000, 2000), radius_ms=120) == 1000


def test_snap_leaves_the_value_alone_outside_the_radius() -> None:
    """A drag into the middle of a long sentence must stay where it was dropped."""
    assert snap(1400, (0, 1000, 2000), radius_ms=120) == 1400


def test_snap_ties_go_to_the_earlier_boundary() -> None:
    # Keeping audio is the safer mistake: it can be trimmed, it cannot be restored.
    assert snap(1050, (0, 1000, 1100), radius_ms=120) == 1000


def test_snap_on_an_empty_grid_is_identity() -> None:
    assert snap(1234, (), radius_ms=120) == 1234


def test_snap_with_a_zero_radius_is_identity() -> None:
    assert snap(1000, (0, 1000), radius_ms=0) == 1000


def test_snap_accepts_a_boundary_exactly_at_the_radius() -> None:
    assert snap(1120, (0, 1000), radius_ms=DEFAULT_SNAP_RADIUS_MS) == 1000


# --- renormalize --------------------------------------------------------


def test_renormalize_does_not_move_edges_that_are_still_on_a_boundary() -> None:
    """The common case: deleting audio inside a block must not touch the block.

    This is the property that makes absolute instants worth storing — if a
    renormalize pass nudged every edge, repeated edits would drift.
    """
    blocks = [Interval(1000, 5000), Interval(5000, 9000)]
    grid = (0, 1000, 5000, 9000, 10000)
    assert renormalize(blocks, grid, duration_ms=10000) == blocks


def test_renormalize_reseats_a_vanished_boundary() -> None:
    # Sentence at 3000 was merged away; the nearest survivor is 2500.
    blocks = [Interval(1000, 3000)]
    grid = (0, 1000, 2500, 9000)
    assert renormalize(blocks, grid, duration_ms=9000) == [Interval(1000, 2500)]


def test_renormalize_extends_rather_than_collapsing_a_block() -> None:
    """Both edges landing on one boundary must not produce a zero-length block.

    Silently dropping the row here would delete a clip the user did not delete,
    in service of an audio edit they can undo.
    """
    blocks = [Interval(1000, 1100)]
    grid = (0, 1000, 4000)
    result = renormalize(blocks, grid, duration_ms=4000)
    assert len(result) == 1
    assert result[0].valid()
    assert result[0] == Interval(1000, 4000)


def test_renormalize_keeps_the_block_alive_at_the_end_of_media() -> None:
    blocks = [Interval(3900, 4000)]
    result = renormalize(blocks, (0, 3900, 4000), duration_ms=4000)
    assert result[0].valid()


def test_renormalize_of_nothing_is_nothing() -> None:
    assert renormalize([], (0, 1000), duration_ms=1000) == []


def test_nearest_boundary_has_no_distance_limit() -> None:
    """Totality matters more than restraint here — the edge must land somewhere."""
    assert nearest_boundary(7000, (0, 1000, 2000)) == 2000
    assert nearest_boundary(-50, (0, 1000)) == 0


# --- what survives an edit ---------------------------------------------


def test_kept_ms_measures_the_rendered_length_not_the_original() -> None:
    """A 10 s block with 2 s deleted plays for 8 s — continuously, not with a cut."""
    block = Interval(0, 10000)
    keeps = [(0, 3000), (5000, 10000)]
    assert block.duration_ms == 10000
    assert kept_ms(block, keeps) == 8000


def test_kept_ms_ignores_keeps_outside_the_block() -> None:
    block = Interval(4000, 6000)
    keeps = [(0, 5000), (7000, 9000)]
    assert kept_ms(block, keeps) == 1000


def test_kept_ms_of_a_fully_deleted_block_is_zero() -> None:
    block = Interval(4000, 6000)
    assert kept_ms(block, [(0, 1000), (8000, 9000)]) == 0


def test_orphaned_blocks_flags_exactly_the_fully_deleted_ones() -> None:
    blocks = [Interval(0, 1000), Interval(1000, 2000), Interval(2000, 3000)]
    keeps = [(0, 1000), (2000, 3000)]
    assert orphaned_blocks(blocks, keeps) == [1]


def test_orphaned_blocks_tolerates_adjacent_keeps() -> None:
    """Guard-ms widening can split one block into several keeps; that is not death."""
    blocks = [Interval(0, 1000)]
    assert orphaned_blocks(blocks, [(0, 400), (400, 1000)]) == []


def test_boundaries_in_use_collects_both_edges() -> None:
    blocks = [Interval(0, 1000), Interval(1000, 2500)]
    assert boundaries_in_use(blocks) == {0, 1000, 2500}


@pytest.mark.parametrize(
    ("value", "grid", "expected"),
    [
        (0, (0, 1000), 0),
        (1000, (0, 1000), 1000),
        (5000, (0, 1000, 5000), 5000),
    ],
)
def test_snap_is_idempotent_on_grid_points(value: int, grid: tuple[int, ...], expected: int) -> None:
    """Snapping a point that is already on the grid must not move it.

    Without this, each renormalize pass could creep a block along the grid.
    """
    assert snap(value, grid, radius_ms=DEFAULT_SNAP_RADIUS_MS) == expected
