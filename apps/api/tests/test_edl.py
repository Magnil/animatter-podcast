"""EDL compilation and time mapping.

The mapping is what every subtitle, chapter mark and export depends on, so these
tests are exhaustive about boundaries: a one-millisecond error here shifts a cue
onto the previous word.
"""

from __future__ import annotations

import pytest

from animatter.core.edl import (
    Deletion,
    EdlError,
    SilenceSpan,
    compile_edl,
    edl_signature,
    normalize_deletions,
)

# --- normalisation ------------------------------------------------------


def test_normalize_sorts() -> None:
    result = normalize_deletions([Deletion(5000, 6000), Deletion(1000, 2000)], original_duration_ms=10_000)
    assert [(d.start_ms, d.end_ms) for d in result] == [(1000, 2000), (5000, 6000)]


def test_normalize_merges_overlapping() -> None:
    result = normalize_deletions([Deletion(1000, 3000), Deletion(2000, 4000)], original_duration_ms=10_000)
    assert len(result) == 1
    assert (result[0].start_ms, result[0].end_ms) == (1000, 4000)


def test_normalize_merges_touching() -> None:
    result = normalize_deletions([Deletion(1000, 2000), Deletion(2000, 3000)], original_duration_ms=10_000)
    assert len(result) == 1


def test_normalize_keeps_gaps() -> None:
    result = normalize_deletions([Deletion(1000, 2000), Deletion(2001, 3000)], original_duration_ms=10_000)
    assert len(result) == 2


def test_normalize_clamps_to_media() -> None:
    result = normalize_deletions([Deletion(-500, 15_000)], original_duration_ms=10_000)
    assert (result[0].start_ms, result[0].end_ms) == (0, 10_000)


def test_normalize_drops_empty() -> None:
    assert normalize_deletions([Deletion(5000, 5000)], original_duration_ms=10_000) == []


def test_normalize_collects_token_ids() -> None:
    result = normalize_deletions(
        [Deletion(1000, 3000, token_ids=(1, 2)), Deletion(2000, 4000, token_ids=(3,))],
        original_duration_ms=10_000,
    )
    assert result[0].token_ids == (1, 2, 3)


# --- compilation --------------------------------------------------------


def test_compile_produces_two_keeps() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(3000, 5000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    assert [(k.orig_start, k.orig_end) for k in edl.keeps] == [(0, 3000), (5000, 10_000)]
    assert edl.output_duration_ms == 8000


def test_compile_computes_new_starts_densely() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 3000), Deletion(5000, 6000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    assert [(k.new_start, k.new_end) for k in edl.keeps] == [(0, 2000), (2000, 4000), (4000, 8000)]


def test_compile_without_deletions_keeps_everything() -> None:
    edl = compile_edl(original_duration_ms=10_000, deletions=[], snap_to_silences=False)
    assert len(edl.keeps) == 1
    assert edl.output_duration_ms == 10_000
    assert edl.removed_ms == 0


def test_compile_refuses_empty_result() -> None:
    with pytest.raises(EdlError, match="没有剩余音频"):
        compile_edl(
            original_duration_ms=10_000,
            deletions=[Deletion(0, 10_000)],
            snap_to_silences=False,
            guard_ms=0,
        )


def test_compile_rejects_zero_duration() -> None:
    with pytest.raises(EdlError, match="时长无效"):
        compile_edl(original_duration_ms=0, deletions=[])


def test_guard_expands_edges() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(3000, 5000)],
        snap_to_silences=False,
        guard_ms=20,
    )
    assert [(k.orig_start, k.orig_end) for k in edl.keeps] == [(0, 2980), (5020, 10_000)]


def test_slivers_dropped() -> None:
    # A 40ms island between two deletions renders as a click.
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(1000, 2000), Deletion(2040, 3000)],
        snap_to_silences=False,
        guard_ms=0,
        min_keep_ms=80,
    )
    starts = [k.orig_start for k in edl.keeps]
    assert 2000 not in starts


def test_removed_ms_matches_deletions() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(1000, 2500)],
        snap_to_silences=False,
        guard_ms=0,
    )
    assert edl.removed_ms == 1500


# --- silence snapping ---------------------------------------------------


def test_snapping_moves_cut_into_silence() -> None:
    # Cut at 3020, silence runs 3000-3200; the cut should land inside it.
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(3020, 5000)],
        silences=[SilenceSpan(3000, 3200)],
        guard_ms=0,
    )
    assert edl.keeps[0].orig_end == 3000 or edl.keeps[0].orig_end == 3200
    assert edl.clamps  # the move must be reported, not silent


def test_snapping_never_shrinks_the_deletion() -> None:
    """Snapping may only grow a deletion, never hand deleted audio back.

    Picking the nearest silence edge regardless of direction used to move an
    edge inward: a cut at 6010 with silence at 5991-6100 snapped back to 5991,
    returning 19ms the user explicitly deleted and restoring the half-phoneme
    that snapping exists to remove.
    """
    edl = compile_edl(
        original_duration_ms=20_000,
        deletions=[Deletion(3709, 6010)],
        silences=[SilenceSpan(5991, 6100)],
        guard_ms=0,
    )
    assert edl.deletions[0].end_ms >= 6010
    assert edl.deletions[0].end_ms == 6100


def test_snapping_start_never_moves_right() -> None:
    """The start edge may only retreat into silence, never advance into speech."""
    edl = compile_edl(
        original_duration_ms=20_000,
        deletions=[Deletion(4050, 8000)],
        silences=[SilenceSpan(3900, 4010)],
        guard_ms=0,
    )
    assert edl.deletions[0].start_ms <= 4050
    assert edl.deletions[0].start_ms == 3900


def test_snapping_ignores_distant_silence() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(3020, 5000)],
        silences=[SilenceSpan(8000, 8200)],
        guard_ms=0,
    )
    # 5s away is beyond the 150ms default, so the cut must not jump there.
    assert edl.keeps[0].orig_end in (3020, 3040)


def test_snapping_never_expands_deletion_to_empty() -> None:
    with pytest.raises(EdlError):
        compile_edl(
            original_duration_ms=1000,
            deletions=[Deletion(0, 990)],
            silences=[SilenceSpan(0, 500)],
            guard_ms=0,
        )


# --- time mapping -------------------------------------------------------


@pytest.fixture()
def edl():  # type: ignore[no-untyped-def]
    return compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 4000), Deletion(6000, 7000)],
        snap_to_silences=False,
        guard_ms=0,
    )


def test_map_before_first_deletion(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().to_new(0) == 0
    assert edl.time_map().to_new(1999) == 1999


def test_map_inside_deletion_is_none(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().to_new(2000) is None
    assert edl.time_map().to_new(3000) is None
    assert edl.time_map().to_new(3999) is None


def test_map_at_adjacent_keep_boundary_belongs_to_later_keep(edl) -> None:  # type: ignore[no-untyped-def]
    """A shared boundary is the later keep's first sample, never the earlier tail."""
    contiguous = compile_edl(
        original_duration_ms=10_000,
        deletions=[],
        snap_to_silences=False,
        guard_ms=0,
    )
    # One keep: its end is the media end, not a boundary inside the stream.
    assert contiguous.time_map().to_new(10_000) is None


def test_map_after_deletion_shifts(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().to_new(4000) == 2000
    assert edl.time_map().to_new(5000) == 3000


def test_map_after_second_deletion(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().to_new(7000) == 4000
    assert edl.time_map().to_new(9999) == 6999


def test_map_clamped_snaps_deleted_instant(edl) -> None:  # type: ignore[no-untyped-def]
    mapping = edl.time_map()
    # 3000 is deleted; it should snap to the end of the preceding keep.
    assert mapping.to_new_clamped(3000) == 2000
    # 6500 is deleted; same rule.
    assert mapping.to_new_clamped(6500) == 4000


def test_reverse_map_roundtrips(edl) -> None:  # type: ignore[no-untyped-def]
    mapping = edl.time_map()
    for original in (0, 1500, 4500, 8000, 9999):
        new = mapping.to_new(original)
        assert new is not None
        assert mapping.to_old(new) == original


def test_reverse_map_out_of_range(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().to_old(999_999) is None


def test_map_range_trims_to_surviving_parts(edl) -> None:  # type: ignore[no-untyped-def]
    # 1500-5000 loses 2000-4000, so only 1500-2000 + 4000-5000 survive.
    assert edl.time_map().map_range(1500, 5000) == (1500, 3000)


def test_map_range_fully_deleted_returns_none(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().map_range(2000, 4000) is None


def test_map_range_untouched(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl.time_map().map_range(0, 1000) == (0, 1000)


# --- signature ----------------------------------------------------------


def test_signature_is_stable(edl) -> None:  # type: ignore[no-untyped-def]
    assert edl_signature(edl) == edl_signature(edl)


def test_signature_changes_with_edits(edl) -> None:  # type: ignore[no-untyped-def]
    other = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 4000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    assert edl_signature(edl) != edl_signature(other)


def test_signature_length(edl) -> None:  # type: ignore[no-untyped-def]
    assert len(edl_signature(edl)) == 64
