"""Box content arithmetic: how a block's play time splits across its images (§19.3.1).

Three things live here because they are arithmetic over integers and every one of them
has a version that is wrong by a few milliseconds in a way nobody notices until the
block's total length disagrees with the film:

* **Gallery pacing.** The chosen rule (round 86) is *proportional*: deleting a second
  of audio shortens every image's dwell by the same factor and keeps the number of
  turns. The split must therefore be computed from cumulative boundaries, never by
  dividing once and multiplying — ``round(d/n) * n`` drifts by up to n/2 ms per block,
  and on a 40-image gallery that is enough to leave a gap the compositor fills with
  whatever happened to be underneath.

* **Segment placement.** A block occupies ``[start, start+output)`` on the *output*
  timeline, not its own edges: its own edges live on the original timeline, and the
  two differ by however much audio was cut inside it.

* **Which asset each segment shows.** Kept as an index into the gallery list rather
  than an id, so this module stays free of database types and can be tested with plain
  integers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: A gallery image shorter than this is a flicker, not a picture. Rather than dropping
#: it (which would silently change the number of turns the user chose), an over-full
#: gallery is collapsed to one segment per image with the dwell it can have.
MIN_DWELL_MS: Final[int] = 120


@dataclass(frozen=True)
class Segment:
    """One stretch of box content: which gallery slot, and when it plays."""

    index: int
    start_ms: int
    duration_ms: int

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


def split_gallery(
    weights: list[int], *, start_ms: int, output_ms: int
) -> list[Segment]:
    """Lay a gallery's images end to end across ``output_ms``.

    Boundaries are computed from the **cumulative** weight and rounded once each, so the
    segments always tile ``[start_ms, start_ms + output_ms)`` exactly. Dividing once
    (``dwell = output // n``) and multiplying back leaves ``output % n`` ms unaccounted
    for, which shows up as the box content ending a few frames before the block does.

    ``weights`` are relative dwell times; all-ones gives the equal split the product
    promises. A non-positive weight is treated as 1 rather than as zero — a zero would
    make an image never appear while still counting as a turn, which reads as a bug.
    """
    if not weights or output_ms <= 0:
        return []

    safe = [max(1, weight) for weight in weights]
    total = sum(safe)

    boundaries: list[int] = []
    accumulated = 0
    for weight in safe:
        accumulated += weight
        # Rounded relative to the block start, not to zero: an absolute round trips
        # over the block's own position and is off by one for odd-length blocks.
        boundaries.append(start_ms + round(output_ms * accumulated / total))

    segments: list[Segment] = []
    cursor = start_ms
    for index, edge in enumerate(boundaries):
        end = edge if index < len(boundaries) - 1 else start_ms + output_ms
        segments.append(Segment(index=index, start_ms=cursor, duration_ms=end - cursor))
        cursor = end
    # The last segment is anchored to the exact end, so any rounding residue lands
    # there instead of trailing past the block.
    return segments


def is_too_fast(segments: list[Segment]) -> bool:
    """True when the block is too short for the number of images it holds.

    The UI warns rather than refusing: the user may legitimately want a rapid flip
    book, and silently dropping images to make them comfortable would change the
    number of turns they set.
    """
    return any(segment.duration_ms < MIN_DWELL_MS for segment in segments)


def gallery_length(items: int, output_ms: int) -> int:
    """How long one image plays under proportional pacing, in whole milliseconds."""
    if items <= 0 or output_ms <= 0:
        return 0
    return output_ms // items


@dataclass(frozen=True)
class FitGeometry:
    """What a fit strategy does to an ``asset_w × asset_h`` image in a ``box_w × box_h`` box.

    Recorded as arithmetic rather than as an ffmpeg string so the four strategies can be
    tested without a renderer. ``scale`` is the factor applied to the whole image before
    any cropping; ``visible`` is the part of the *scaled* image that survives.
    """

    scale: float
    visible_w: int
    visible_h: int
    letterboxed: bool


def fit_geometry(
    fit: str, *, asset_w: int, asset_h: int, box_w: int, box_h: int
) -> FitGeometry:
    """Resolve one fit strategy into numbers.

    ``contain`` and ``blur_fill`` both scale to *fit inside*, so the whole image is
    visible and the leftover area is letterbox. ``cover`` scales to *fill*, so the
    image is cropped instead. ``stretch`` ignores the aspect ratio entirely.

    **``cover`` keeps the top of the picture** (``crop=w:h:0:0`` in ``media/box_track``),
    not the middle. Material is screenshots and posters: the subject sits at the top and
    the overflow is a footer or dead margin, so a centred crop throws away the top *and*
    keeps the bottom. The horizontal case follows the same rule — a too-wide picture loses
    its right edge rather than both.

    Both blur_fill members matter: its blurred backdrop is a ``cover`` (it must reach
    every edge) while its foreground is a ``contain`` (it must not lose any pixel).
    """
    if asset_w <= 0 or asset_h <= 0 or box_w <= 0 or box_h <= 0:
        return FitGeometry(scale=1.0, visible_w=box_w, visible_h=box_h, letterboxed=False)

    cover = max(box_w / asset_w, box_h / asset_h)
    inside = min(box_w / asset_w, box_h / asset_h)

    if fit == "stretch":
        return FitGeometry(scale=cover, visible_w=box_w, visible_h=box_h, letterboxed=False)
    if fit == "cover":
        return FitGeometry(scale=cover, visible_w=box_w, visible_h=box_h, letterboxed=False)
    if fit in {"contain", "blur_fill"}:
        scaled_w = round(asset_w * inside)
        scaled_h = round(asset_h * inside)
        return FitGeometry(
            scale=inside,
            visible_w=min(box_w, scaled_w),
            visible_h=min(box_h, scaled_h),
            letterboxed=scaled_w != box_w or scaled_h != box_h,
        )
    # Unknown strategy falls back to the one that never loses content.
    scaled_w = round(asset_w * inside)
    scaled_h = round(asset_h * inside)
    return FitGeometry(
        scale=inside,
        visible_w=min(box_w, scaled_w),
        visible_h=min(box_h, scaled_h),
        letterboxed=scaled_w != box_w or scaled_h != box_h,
    )
