"""Cut a film into chunks that one ffmpeg process can render (§19.27).

Why chunks exist
----------------
A film is one filtergraph, and the material chain puts **one branch per segment** in it. ffmpeg's
filtergraph is push-based: ``concat`` drains its inputs in order, so a branch that is not yet
current still receives frames and queues them. Peak memory therefore tracks the number of
branches, and barely tracks the output resolution at all. Measured on the 费那奇MV project
(68 segments, 56 distinct inputs, 3696 s of film) at 1920x1080:

======  ======  ==============================
branches  inputs  peak RSS
======  ======  ==============================
 6        6      1222 MB
20       16      2686 MB
40       32      5219 MB
68       56      9196 MB (and 8842 MB at 720p)
======  ======  ==============================

At 68 branches the encode dies with ``Cannot allocate memory`` on a 16 GB machine, and
``-threads 1`` per input only buys ~40 %. Splitting the timeline is the fix that does not cost
quality: each chunk is encoded normally and the chunks are ``-c copy`` concatenated.

What must not drift
-------------------
Chunk boundaries are **segment boundaries**, so no picture is split. Each chunk asks for an exact
**frame count** rather than a duration, because at 24 fps one frame is 41.667 ms and rounding a
duration per chunk would accumulate over a 90-minute film. Subtitles are re-cut per chunk in
chunk-local time.

This module is the pure part: no IO, no ffmpeg, no session. ``video_render`` does the work and
calls these for the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError
from animatter.media import box_track
from animatter.media.subtitle_ass import AssCue

#: ASS cue times are in centiseconds, so a cue's start snapped to this grid survives
#: re-basing without ever starting a frame early.
_CENTISECOND_MS: Final[int] = 10


@dataclass(frozen=True)
class Chunk:
    """One slice of the output timeline, in both segments and milliseconds.

    ``first``/``last`` are half-open indices into the plan's segment list, so the two halves of
    a split plan are sliced with the same pair — that is what keeps the lower and upper chains
    aligned segment for segment.
    """

    start_ms: int
    end_ms: int
    first: int
    last: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def frames(self, fps: int) -> int:
        """How many frames this chunk must contain. Counted from the ends, never the length.

        Deriving it as ``round(duration * fps / 1000)`` would let two neighbouring chunks round
        in opposite directions and leave a gap or a repeat at the join. Subtracting two rounded
        boundaries makes the chunks tile the film exactly.
        """
        start = round(self.start_ms * fps / 1000)
        end = round(self.end_ms * fps / 1000)
        return max(1, end - start)

    def start_frame(self, fps: int) -> int:
        return round(self.start_ms * fps / 1000)


def boundaries_of(segments: list[box_track.BoxSegment]) -> list[int]:
    """Cumulative segment boundaries, ``len(segments) + 1`` long, starting at 0."""
    out = [0]
    for segment in segments:
        out.append(out[-1] + segment.duration_ms)
    return out


def split(
    segments: list[box_track.BoxSegment],
    *,
    max_segments: int,
    max_ms: int,
) -> list[Chunk]:
    """Greedy chunking: as many segments as fit under both limits, never splitting one.

    A segment is indivisible because splitting it would mean decoding the same material twice
    and re-seeking a clip mid-way — both cost more than the memory they save. ``max_ms`` is
    therefore a *soft* limit: the 费那奇MV project has a single 591 s 全屏 block, and a chunk
    containing it is 591 s long however the limits are set.

    Raises when there is nothing to chunk: an empty plan is the caller's decision to make (a
    film with a background and subtitles but no material is a legitimate export), and silently
    returning ``[]`` would produce a film with no picture.
    """
    if not segments:
        raise AppError("box_track_empty", "素材框里没有任何内容", hint="先往时间轴上放一个素材块。")
    boundaries = boundaries_of(segments)
    chunks: list[Chunk] = []
    cursor = 0
    while cursor < len(segments):
        take = 1
        while (
            cursor + take < len(segments)
            and take < max_segments
            and boundaries[cursor + take + 1] - boundaries[cursor] <= max_ms
        ):
            take += 1
        chunks.append(
            Chunk(
                start_ms=boundaries[cursor],
                end_ms=boundaries[cursor + take],
                first=cursor,
                last=cursor + take,
            )
        )
        cursor += take
    return chunks


def whole(duration_ms: int) -> list[Chunk]:
    """A single chunk covering the film — the shape an empty material chain renders in.

    Also the shape a film shorter than one chunk renders in, which keeps the export path the
    same one the tests exercise.
    """
    if duration_ms <= 0:
        raise AppError("video_duration_invalid", "视频时长为 0", hint="先渲染音频。")
    return [Chunk(start_ms=0, end_ms=duration_ms, first=0, last=0)]


def rebase(
    segments: list[box_track.BoxSegment],
    sources: list[Path],
) -> tuple[list[box_track.BoxSegment], list[Path]]:
    """Renumber a chunk's segment inputs to start at 0, keeping only the files it uses.

    The plan numbers inputs across the whole film; a chunk declares only its own. Every segment
    of the chunk is kept — including the transparent placeholders — because the chain's
    ``concat`` needs one input per segment, and placeholders cost no ffmpeg input at all.

    First-appearance order, not the plan's order, so the declared ``-i`` list matches the
    renumbered ``input_index`` values exactly.
    """
    order: dict[int, int] = {}
    ordered: list[Path] = []
    for segment in segments:
        if segment.kind == box_track.KIND_TRANSPARENT:
            continue
        if segment.input_index not in order:
            order[segment.input_index] = len(ordered)
            ordered.append(sources[segment.input_index])
    out = [
        box_track.BoxSegment(
            input_index=order[segment.input_index]
            if segment.kind != box_track.KIND_TRANSPARENT
            else 0,
            duration_ms=segment.duration_ms,
            kind=segment.kind,
            fit=segment.fit,
            crop=segment.crop,
            loop=segment.loop,
            placement=segment.placement,
        )
        for segment in segments
    ]
    return out, ordered


def slice_cues(cues: list[AssCue], chunk: Chunk) -> list[AssCue]:
    """The cues that overlap ``chunk``, re-based to chunk-local milliseconds.

    A cue that straddles the boundary is **clipped**, not dropped and not duplicated: it will be
    drawn on both chunks, each showing its own part. Dropping it would lose speech at every
    join; duplicating it unclipped would draw the whole sentence twice, once on a chunk where
    the audio no longer says it.

    The start is snapped down to a centisecond — ASS's own resolution — so a cue can never end
    up starting one frame later on the chunk than it did in the film. Both ends are clamped into
    the chunk, because libass happily draws a cue with a negative start and then it appears at
    the wrong place.
    """
    out: list[AssCue] = []
    for cue in cues:
        if cue.end_ms <= chunk.start_ms or cue.start_ms >= chunk.end_ms:
            continue
        start = max(0, cue.start_ms - chunk.start_ms)
        start -= start % _CENTISECOND_MS
        end = min(chunk.duration_ms, cue.end_ms - chunk.start_ms)
        if end <= start:
            # A cue shorter than the snap after clipping: keep one centisecond rather than
            # dropping it, so a boundary-crossing cue is still visible on both sides.
            end = start + _CENTISECOND_MS
        out.append(AssCue(start_ms=start, end_ms=end, text=cue.text))
    return out
