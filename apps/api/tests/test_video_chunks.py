"""Chunking the export timeline (§19.27): arithmetic that must not drift.

The 费那奇MV export died with `Cannot allocate memory` because one filtergraph held 68 material
branches. The fix renders a few segments per ffmpeg process and `-c copy` concatenates them. That
only works if the arithmetic is exact, so the properties are pinned here:

* chunks tile the film with no gap and no overlap, and never split a segment;
* a chunk's frame count is derived from two rounded boundaries, so neighbours cannot round in
  opposite directions and leave a hole at the join;
* subtitles are re-cut per chunk, with a cue that straddles a join clipped on both sides rather
  than dropped (losing speech) or duplicated unclipped (drawing a sentence over the wrong audio).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from animatter.errors import AppError
from animatter.media import box_track, video_chunks
from animatter.media.subtitle_ass import AssCue


def segments(*durations: int) -> list[box_track.BoxSegment]:
    return [
        box_track.BoxSegment(input_index=index, duration_ms=value, kind=box_track.KIND_IMAGE)
        for index, value in enumerate(durations)
    ]


# --- splitting -----------------------------------------------------------


def test_chunks_tile_the_film_exactly() -> None:
    """No gap and no overlap: every millisecond belongs to exactly one chunk."""
    plan = segments(*([1000] * 20))
    chunks = video_chunks.split(plan, max_segments=6, max_ms=10_000)
    assert chunks[0].start_ms == 0
    assert chunks[-1].end_ms == 20_000
    for left, right in itertools.pairwise(chunks):
        assert left.end_ms == right.start_ms
        assert left.last == right.first
    assert sum(chunk.duration_ms for chunk in chunks) == 20_000


def test_no_segment_is_ever_split() -> None:
    plan = segments(500, 7000, 300, 400, 400, 400)
    chunks = video_chunks.split(plan, max_segments=2, max_ms=2000)
    # The 7000 ms segment is longer than max_ms on its own, so it gets a chunk to itself rather
    # than being cut: splitting it would mean decoding the same material twice.
    assert [(chunk.first, chunk.last) for chunk in chunks] == [
        (0, 1),
        (1, 2),
        (2, 4),
        (4, 6),
    ]
    assert chunks[1].duration_ms == 7000


def test_max_segments_bounds_the_branch_count() -> None:
    """The whole point: the chunk's branch count is what the memory cost tracks."""
    chunks = video_chunks.split(segments(*([100] * 68)), max_segments=6, max_ms=10**9)
    assert [chunk.last - chunk.first for chunk in chunks] == [6] * 11 + [2]


def test_the_film_gets_one_chunk_when_it_fits() -> None:
    chunks = video_chunks.split(segments(1000, 1000), max_segments=6, max_ms=10_000)
    assert len(chunks) == 1
    assert (chunks[0].start_ms, chunks[0].end_ms) == (0, 2000)


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(AppError):
        video_chunks.split([], max_segments=6, max_ms=10_000)


def test_a_film_with_no_material_gets_one_chunk() -> None:
    """A background-and-subtitles-only export is legitimate, and `whole` is its one chunk."""
    assert video_chunks.whole(5000) == [
        video_chunks.Chunk(start_ms=0, end_ms=5000, first=0, last=0)
    ]


# --- frame counts --------------------------------------------------------


def test_frame_counts_tile_the_film_without_rounding_gaps() -> None:
    """The reason `frames()` subtracts two rounded boundaries instead of rounding the length.

    24 fps means one frame is 41.667 ms, so a chunk boundary almost never lands on a whole
    millisecond. Rounding each chunk's *duration* would let two neighbours round the same
    boundary in opposite directions: one frame lost or repeated at the join.
    """
    plan = segments(*([101] * 97))
    chunks = video_chunks.split(plan, max_segments=5, max_ms=10**9)
    fps = 24
    total = sum(chunk.frames(fps) for chunk in chunks)
    # 97 segments of 101 ms = 9797 ms = 235.1 frames. The telescope must land exactly on the
    # film's own rounded frame count, which is the property that makes the joined file match
    # `duration_ms` frame for frame.
    assert total == round(9797 * fps / 1000) == 235
    # And the boundary check the formula exists for: consecutive chunks share a frame index.
    for left, right in itertools.pairwise(chunks):
        assert left.start_frame(fps) + left.frames(fps) == right.start_frame(fps)


def test_a_chunk_is_never_zero_frames() -> None:
    """A 1 ms chunk would be `-frames:v 0`, which writes an empty file."""
    assert video_chunks.Chunk(start_ms=0, end_ms=1, first=0, last=1).frames(24) == 1


# --- input renumbering ---------------------------------------------------


def test_a_chunk_only_declares_the_files_it_shows() -> None:
    """The memory fix in one assertion: a six-segment chunk declares six inputs, not 56."""
    sources = [Path(f"asset-{index}.png") for index in range(10)]
    plan = [
        box_track.BoxSegment(input_index=index, duration_ms=1000, kind=box_track.KIND_IMAGE)
        for index in range(10)
    ]
    rebuilt, ordered = video_chunks.rebase(plan[4:7], sources)
    assert ordered == sources[4:7]
    assert [segment.input_index for segment in rebuilt] == [0, 1, 2]


def test_placeholders_declare_no_input_and_keep_their_slot() -> None:
    """A transparent placeholder is a `color` filter: it must not shift the input numbering."""
    sources = [Path("a.png")]
    plan = [
        box_track.BoxSegment(input_index=0, duration_ms=1000, kind=box_track.KIND_TRANSPARENT),
        box_track.BoxSegment(input_index=0, duration_ms=1000, kind=box_track.KIND_IMAGE),
    ]
    rebuilt, ordered = video_chunks.rebase(plan, sources)
    assert ordered == sources
    assert rebuilt[0].kind == box_track.KIND_TRANSPARENT
    assert rebuilt[1].input_index == 0


def test_a_file_used_twice_is_declared_once() -> None:
    sources = [Path("a.png"), Path("b.png")]
    plan = [
        box_track.BoxSegment(input_index=1, duration_ms=1000, kind=box_track.KIND_IMAGE),
        box_track.BoxSegment(input_index=0, duration_ms=1000, kind=box_track.KIND_IMAGE),
        box_track.BoxSegment(input_index=1, duration_ms=1000, kind=box_track.KIND_IMAGE),
    ]
    rebuilt, ordered = video_chunks.rebase(plan, sources)
    assert ordered == [sources[1], sources[0]]
    assert [segment.input_index for segment in rebuilt] == [0, 1, 0]


# --- subtitles -----------------------------------------------------------


def test_cues_are_rebased_to_chunk_local_time() -> None:
    chunk = video_chunks.Chunk(start_ms=10_000, end_ms=20_000, first=0, last=1)
    cues = [AssCue(start_ms=11_000, end_ms=12_000, text="hello")]
    assert video_chunks.slice_cues(cues, chunk) == [
        AssCue(start_ms=1_000, end_ms=2_000, text="hello")
    ]


def test_a_cue_across_the_join_is_clipped_on_both_sides_not_dropped() -> None:
    """Losing it would lose speech at every join; duplicating it unclipped would draw the whole
    sentence over the wrong audio."""
    left = video_chunks.Chunk(start_ms=0, end_ms=10_000, first=0, last=1)
    right = video_chunks.Chunk(start_ms=10_000, end_ms=20_000, first=1, last=2)
    cues = [AssCue(start_ms=9_000, end_ms=11_000, text="跨过接缝")]
    assert video_chunks.slice_cues(cues, left) == [
        AssCue(start_ms=9_000, end_ms=10_000, text="跨过接缝")
    ]
    assert video_chunks.slice_cues(cues, right) == [
        AssCue(start_ms=0, end_ms=1_000, text="跨过接缝")
    ]


def test_a_cue_outside_the_chunk_is_absent() -> None:
    chunk = video_chunks.Chunk(start_ms=10_000, end_ms=20_000, first=0, last=1)
    cues = [
        AssCue(start_ms=0, end_ms=5_000, text="before"),
        AssCue(start_ms=25_000, end_ms=30_000, text="after"),
    ]
    assert video_chunks.slice_cues(cues, chunk) == []


def test_a_cue_start_is_snapped_down_so_it_never_starts_late() -> None:
    """ASS's own resolution is a centisecond; snapping down keeps the cue from moving right."""
    chunk = video_chunks.Chunk(start_ms=0, end_ms=10_000, first=0, last=1)
    cues = [AssCue(start_ms=1_234, end_ms=2_000, text="x")]
    assert video_chunks.slice_cues(cues, chunk)[0].start_ms == 1_230


def test_a_chunk_local_cue_never_has_a_negative_time() -> None:
    """libass draws a negative-start cue at the wrong place; clamping is cheaper than trusting."""
    chunk = video_chunks.Chunk(start_ms=5_000, end_ms=10_000, first=0, last=1)
    cues = [AssCue(start_ms=4_000, end_ms=6_000, text="straddles the start")]
    sliced = video_chunks.slice_cues(cues, chunk)[0]
    assert sliced.start_ms == 0
    assert sliced.end_ms == 1_000
