"""Chunk planning for long episodes (decision D8).

Speaker diarization is only reliable up to ~2 hours, so anything longer is split
at silence midpoints. Cutting mid-vowel is what makes a seam audible, hence the
preference order: silence midpoint → nearest silence in a ±5min window → hard cut
with ``quality='forced'`` recorded for the UI to warn about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from animatter.log import get_logger
from animatter.media import ffmpeg

log = get_logger(__name__)


@dataclass(frozen=True)
class Segment:
    index: int
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class ChunkPlan:
    segments: list[Segment]
    quality: str  # 'single' | 'silence' | 'forced'

    @property
    def is_chunked(self) -> bool:
        return len(self.segments) > 1


def plan(
    ffmpeg_path: str,
    media_path: Path,
    duration_ms: int,
    *,
    threshold_ms: int,
    target_ms: int,
) -> ChunkPlan:
    """Decide whether to split, and where."""
    if duration_ms <= threshold_ms:
        return ChunkPlan(segments=[Segment(0, 0, duration_ms)], quality="single")

    silences = ffmpeg.detect_silence(ffmpeg_path, media_path)
    log.info("chunk.silences", count=len(silences), duration_ms=duration_ms)

    points, quality = ffmpeg.choose_chunk_points(duration_ms, silences, target_ms=target_ms)
    ranges = ffmpeg.build_segments(duration_ms, points)
    segments = [Segment(index=i, start_ms=start, end_ms=end) for i, (start, end) in enumerate(ranges)]

    log.info("chunk.planned", segments=len(segments), quality=quality, points=points)
    return ChunkPlan(segments=segments, quality=quality)


def slice_audio(ffmpeg_path: str, source: Path, out_dir: Path, plan_result: ChunkPlan) -> list[Path]:
    """Materialise each segment as its own mono 16 kHz mp3."""
    paths: list[Path] = []
    for segment in plan_result.segments:
        if not plan_result.is_chunked:
            paths.append(source)
            continue
        target = out_dir / f"seg{segment.index:03d}.mp3"
        paths.append(ffmpeg.extract_segment(ffmpeg_path, source, target, segment.start_ms, segment.end_ms))
    return paths
