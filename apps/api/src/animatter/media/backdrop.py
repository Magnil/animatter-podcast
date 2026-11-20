"""The blurred backdrop for `blur_fill`/`blur_crop`, baked once per segment (§19.31.3).

**Why this cannot be CSS.** The film's backdrop is
``scale=…increase, crop=W:H:0:0, boxblur=luma_radius=20:luma_power=2, eq=brightness=-0.10``.
Three of those four steps have no CSS equivalent:

* ``boxblur`` is a **box** kernel run twice — its falloff is triangular, not gaussian, so
  ``filter: blur(20px)`` is visibly a different picture at the edges of the box;
* ``eq=brightness=-0.10`` is a luma transform, not ``filter: brightness(90%)``, which
  multiplies in linear-ish sRGB space and lands on different values;
* ``crop=W:H:0:0`` is top-left anchored while ``object-fit: cover`` centres.

A preview that disagrees with the film exactly inside the one effect the user is judging is
the failure this whole design avoids, so the backdrop is baked and the browser just places it.

**The foreground stays live.** Only the backdrop is pre-rendered: `contain` on top of it is
native CSS, and for `blur_crop` the foreground is not scaled at all, which is precisely
``width: auto`` in a centred box.

**One exception, reported not hidden.** A *video* segment's backdrop moves in the film and
cannot move here — freezing it is the only option short of a proxy render (P2). The plan marks
those segments with ``backdrop_frozen`` so the UI can say so (§19.32), and on the sample it is
one segment of 24.2 s.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from animatter.errors import AppError
from animatter.media import box_track

#: Bumped whenever the filter spelling below changes. The file name carries the plan digest,
#: which carries this number through `video_plan._digest`, so an upgraded build re-bakes instead
#: of serving yesterday's blur (§19.11 trap 24).
BACKDROP_VERSION = 1

#: **PNG, not JPEG** — the opposite of what a size argument says, and the measurement decided it.
#:
#: A blurred backdrop is smooth, so JPEG looked like the obvious choice (41 KB against 200 KB).
#: But JPEG is written as full-range ``yuvj420p`` while the film's chain keeps the backdrop in
#: limited-range ``yuv420p`` through ``eq``, and the round trip shifts values by more than the
#: 24/255 the consistency check tolerates: measured **4.5 % of the visible ring differing** on a
#: `blur_fill` segment — a real, visible band around the sharp foreground. Lossless output removes
#: the whole class of error, and 26 backdrops of ~200 KB is 5 MB against a preview that already
#: caches 772 cue bands.
_LOSSLESS = True


def filters(*, width: int, height: int) -> str:
    """The backdrop chain, spelled exactly as :func:`box_track._blurred_backdrop` spells it.

    Imported rather than copied where possible: ``_blurred_backdrop`` takes labels because it
    lives inside a larger graph, so this wraps its constants. A second literal
    ``luma_radius=20`` here is how the preview and the film start disagreeing about the blur.
    """
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:0:0,"
        f"boxblur=luma_radius={box_track._BLUR_RADIUS}:luma_power={box_track._BLUR_POWER},"
        f"{box_track._BACKDROP_DIM},setsar=1"
    )


def bake(
    ffmpeg: str,
    *,
    target: Path,
    source: Path,
    kind: str,
    width: int,
    height: int,
    timeout: int = 120,
) -> Path:
    """Render one segment's backdrop into ``target`` (a lossless PNG) at the box's size.

    ``kind`` decides how the frame is read: a still becomes one frame, a video is read at its
    **first** frame (there is one backdrop per segment, and the segment's own start is the frame
    the viewer sees first — a frozen backdrop taken from anywhere else would be doubly wrong).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    read = (
        ["-i", str(source)]
        if kind == box_track.KIND_IMAGE
        else ["-ss", "0", "-i", str(source)]
    )
    completed = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            *read,
            "-vf",
            filters(width=width, height=height),
            "-frames:v",
            "1",
            "-update",
            "1",
            # No `-q:v`: this is lossless, and a quality flag on a PNG is silently ignored.
            "-f",
            "image2",
            str(target),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise AppError(
            "backdrop_bake_failed",
            "这个素材的模糊底没能生成",
            hint=detail[-1] if detail else "换一张图或重新导入这个素材，再试一次。",
            status=500,
        )
    return target
