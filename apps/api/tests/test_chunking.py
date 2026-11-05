"""Chunk planning: where to cut a long episode (decision D8).

Wrong cut points are the difference between an inaudible seam and an audible
one, so the preference order (silence midpoint → nearest silence → hard cut) is
tested explicitly.
"""

from __future__ import annotations

import pytest

from animatter.media.ffmpeg import SilenceSpan, build_segments, choose_chunk_points, detect_silence


def test_short_file_is_not_chunked() -> None:
    points, quality = choose_chunk_points(3_600_000, [], target_ms=5_400_000)
    assert points == []
    assert quality == "silence"


def test_cut_prefers_silence_midpoint() -> None:
    silences = [SilenceSpan(5_390_000, 5_410_000)]  # midpoint 5_400_000
    points, quality = choose_chunk_points(10_800_000, silences, target_ms=5_400_000)
    assert quality == "silence"
    assert points[0] == 5_400_000


def test_cut_uses_nearest_silence_within_window() -> None:
    silences = [SilenceSpan(5_200_000, 5_210_000)]  # midpoint 5_205_000, 195s early
    points, quality = choose_chunk_points(10_800_000, silences, target_ms=5_400_000)
    assert quality == "silence"
    assert points[0] == 5_205_000


def test_cut_falls_back_to_hard_cut_and_flags_forced() -> None:
    far_away = [SilenceSpan(0, 1_000)]  # ~90 minutes from the target
    points, quality = choose_chunk_points(10_800_000, far_away, target_ms=5_400_000)
    assert quality == "forced"
    assert points[0] == 5_400_000


def test_multiple_targets() -> None:
    points, quality = choose_chunk_points(16_200_000, [], target_ms=5_400_000)
    assert quality == "forced"
    assert points == [5_400_000, 10_800_000]


def test_picks_closest_among_candidates() -> None:
    silences = [
        SilenceSpan(5_150_000, 5_160_000),
        SilenceSpan(5_390_000, 5_400_000),  # midpoint 5_395_000 — closest
        SilenceSpan(5_600_000, 5_610_000),
    ]
    points, _ = choose_chunk_points(10_800_000, silences, target_ms=5_400_000)
    assert points[0] == 5_395_000


# --- build_segments -----------------------------------------------------


def test_build_segments_covers_whole_file() -> None:
    segments = build_segments(10_000, [4_000, 7_000])
    assert segments == [(0, 4_000), (4_000, 7_000), (7_000, 10_000)]


def test_build_segments_no_points() -> None:
    assert build_segments(10_000, []) == [(0, 10_000)]


def test_build_segments_ignores_out_of_range_points() -> None:
    assert build_segments(10_000, [0, 5_000, 10_000]) == [(0, 5_000), (5_000, 10_000)]


def test_build_segments_sorted_input() -> None:
    assert build_segments(10_000, [7_000, 3_000]) == [(0, 3_000), (3_000, 7_000), (7_000, 10_000)]


def test_detect_silence_missing_binary_raises_clear_error() -> None:
    from pathlib import Path

    from animatter.errors import AppError

    # A missing ffmpeg is a configuration problem and must say so, not silently
    # report "no silences" and let the render fail later with a worse message.
    with pytest.raises(AppError) as caught:
        detect_silence("animatter-missing-ffmpeg", Path("nope.mp3"))
    assert caught.value.code == "ffmpeg_missing"
    assert "AM_FFMPEG_PATH" in caught.value.hint
