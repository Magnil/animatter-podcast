"""Baking one subtitle cue into a transparent PNG band, for the DOM preview.

**Why pixels at all.** The film's subtitles are drawn by libass. CSS text cannot reproduce
libass's wrapping, kerning, outline and shadow, and a preview that disagrees with the film
about the subtitle is worse than no preview — it is the thing the user is checking.

**Why a bottom band and not the glyph bounding box.** §19.31.4 originally proposed cropping to
each cue's ink bbox. That needs one bake per cue before the *rect* is known, and the rect is
what the browser must have first to lay the layer out. A band whose height depends only on
the **style** (so the plan can publish the rect without baking anything) fixes that, and
costs nothing else: the band is a plain crop of the full canvas, so it is pixel-identical by
construction.

**Three mistakes that cost a round each, all measured (round 128):**

1. Without ``alpha=1`` the ``ass`` filter *replaces* the frame instead of drawing onto it:
   1920×1080 came out fully opaque (2 073 600 px at alpha=255) — a black slab over the film.
2. The transparent canvas must be created **inside** the filtergraph. A ``color=c=black@0.0``
   *input* is negotiated as yuv420p and the alpha is gone before ``format=rgba`` runs; same for
   a fully transparent PNG input (`ass` still replaced it). ``format=rgba,
   colorchannelmixer=aa=0`` in the same simple graph works.
3. A short canvas handed to libass with ``original_size`` is **not** a crop: libass re-fits
   PlayRes into the frame, so the text is squashed and re-wrapped. Measured 46 292–107 073
   differing pixels against the real crop. Render at canvas size, then ``crop``.

Cost, measured on this machine (ffmpeg 7.1, median of 3): **65 ms** per band, of which
**33 ms is ffmpeg's own startup** and ~5 ms is the PNG encode. That is why cues are baked on
demand rather than for the whole film: 772 of them would be ~50 s of work and ~50 MB of disk
to serve a preview that shows at most three at a time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from animatter.media.subtitle_ass import AssCue, SubtitleStyle, wrap_cjk, write_ass
from animatter.media.video import escape_filter_path

#: libass advances a line by this much, in em. Measured on Microsoft YaHei at 52 px: one
#: line of ink is 45 px tall and the *next* line starts 62 px lower, i.e. 1.192 em.
LINE_ADVANCE_EM = 1.25
#: Room for the outline and the drop shadow, which sit outside the glyph box, plus a couple
#: of rows of slack. Both scale with the style's own numbers rather than being magic.
_EXTRA_MIN = 4


def band_height(text: str, style: SubtitleStyle, *, play_w: int, canvas_h: int) -> int:
    """How tall the bottom band must be to contain this cue's ink.

    Depends only on the style and the wrapped line count, so the **plan** can publish it for
    every cue without baking one. Deliberately generous: 24 px of unused transparent rows cost
    nothing, while one clipped descender is a visible defect.
    """
    lines = len(wrap_cjk(text, style=style, play_w=play_w))
    advance = round(style.font_size * LINE_ADVANCE_EM)
    outside = round(style.outline + style.shadow) + _EXTRA_MIN
    return min(canvas_h, style.margin_v + lines * advance + outside)


def band_top(text: str, style: SubtitleStyle, *, play_w: int, canvas_h: int) -> int:
    """The ``y`` of the band — the ink is bottom-anchored, so the band hugs the bottom."""
    return canvas_h - band_height(text, style, play_w=play_w, canvas_h=canvas_h)


def bake(
    ffmpeg: str,
    *,
    target: Path,
    text: str,
    style: SubtitleStyle,
    canvas_w: int = 1920,
    canvas_h: int = 1080,
    timeout: int = 60,
) -> Path:
    """Render one cue into a transparent band PNG at ``target``.

    ``-update 1`` is required next to ``-frames:v 1``: the image2 muxer otherwise refuses the
    first frame for trying to write a second one to the same name ("Cannot write more than one
    file with the same name"), an error about the *output* that reads like a filter problem.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    height = band_height(text, style, play_w=canvas_w, canvas_h=canvas_h)
    top = canvas_h - height
    # The ASS is written next to the target and removed after: it carries the cue's own text,
    # and leaving one file per cue would litter the bake directory with 772 one-line files.
    script = target.with_suffix(".cue.ass")
    write_ass(script, [AssCue(start_ms=0, end_ms=4_000, text=text)], style=style)
    try:
        filters = (
            # The transparent canvas first, then the text drawn *onto* it with alpha kept.
            # The path must be escaped for the filter argument: `C:` reads as an option
            # separator, and the failure names the filter, not the path (§19.11).
            f"format=rgba,colorchannelmixer=aa=0,"
            f"ass=filename='{escape_filter_path(script)}':alpha=1,"
            f"crop={canvas_w}:{height}:0:{top}"
        )
        completed = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={canvas_w}x{canvas_h}:r=24:d=1",
                "-vf",
                filters,
                "-frames:v",
                "1",
                "-update",
                "1",
                "-pix_fmt",
                "rgba",
                "-c:v",
                "png",
                "-compression_level",
                "1",
                str(target),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    finally:
        script.unlink(missing_ok=True)

    if completed.returncode != 0:
        from animatter.errors import AppError

        detail = (completed.stderr or "").strip().splitlines()
        raise AppError(
            "cue_bake_failed",
            "这条字幕没能烘焙出来",
            hint=detail[-1] if detail else "检查 ffmpeg 是否可用，以及字幕字体是否已安装。",
            status=500,
        )
    return target
