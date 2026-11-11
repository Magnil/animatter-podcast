"""Avatar track rendering: lit/dim stills into one RGBA clip (§19.5).

Measured facts that shaped this, all on the sample 236×249 avatar PNGs:

* the **concat demuxer** with per-file ``duration`` produces a variable-frame-rate clip
  whose frames land at the right instants (checked: 0.0 / 2.0 / 5.0 / 10.0 s on a
  2+3+5 s script). That is the whole mechanism: N stills in, one short video out.
* **qtrle** (QuickTime Animation, ARGB) is the right codec: 1200 segments / 600 s in
  **1.5 s** and 55 MB. The PNG codec took 5.9 s and produced **338 MB** for the same
  clip, because it stores a full lossless frame each time. Both are lossless; only one is
  usable inside an export that already writes a 1 GB mp4.
* the concat list must be written **without a BOM**. With one, ffmpeg reports
  ``Line 1: unknown keyword 'file'`` — which names the keyword, not the encoding, and
  sends you looking at the file paths.
* every ``duration`` is **rounded up to a whole frame** at the output rate (1.500 s became
  1.520 at 25 fps), so durations are snapped cumulatively here instead of per piece.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.core.video_avatar import TrackPiece
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media.video import FPS

log = get_logger(__name__)

#: Lossless RGBA. ``qtrle`` because it is the only one measured to be both fast (1.5 s /
#: 600 s of track) and compact (55 MB) for a clip of still images.
CODEC: Final[str] = "qtrle"
PIX_FMT: Final[str] = "argb"


@dataclass(frozen=True)
class AvatarPiece:
    """One stretch of an avatar track, resolved to the file that should be on screen."""

    path: Path
    duration_ms: int


def _escape(path: Path) -> str:
    """A path the concat parser will read back.

    Absolute, forward slashes, escaped single quote. **Absolute matters**: the demuxer
    resolves relative paths against the *script's* directory, not the process's working
    directory, so a relative entry silently points at ``<workdir>/data/...``. Measured: it
    fails as "视频渲染失败" with the ffmpeg complaint about an unreadable input, which reads
    like a missing file rather than a path-base mistake.
    """
    return path.resolve().as_posix().replace("'", "'\\''")


def write_concat_list(target: Path, pieces: list[AvatarPiece], *, fps: int = FPS) -> Path:
    """Write the concat demuxer script for one avatar track.

    Three things here are not obvious, and each one produces a *silent* timing error:

    **Durations are snapped to the frame grid, cumulatively.** Measured: the demuxer rounds
    each ``duration`` up to a whole frame at the output rate, so a 1.500 s segment became
    1.520 at 25 fps. Rounding every piece independently accumulates that error down a track
    with hundreds of pieces, so here the *boundary* is snapped and each duration is the gap
    between two snapped boundaries. The last boundary is the film's end, never a drift.

    **No BOM.** With one, ffmpeg reports ``Line 1: unknown keyword 'file'``, which names the
    keyword rather than the encoding and sends you looking at the file paths.

    **The final piece is listed twice.** Without a trailing entry the last segment's
    duration is not honoured — measured 2.56 s for a 3.00 s script — because the demuxer
    needs a following timestamp to know how long the last frame stays. The duplicate costs
    one frame and makes the track end slightly *after* the film, which is the harmless
    direction: the film's own ``-t`` bounds the encode, whereas a track that ends early
    freezes the overlay's last frame.
    """
    if not pieces:
        raise AppError("avatar_track_empty", "头像轨没有任何内容")

    frame_ms = 1000 / max(1, fps)
    lines: list[str] = []
    cursor_ms = 0
    previous_boundary_ms = 0.0
    for piece in pieces:
        cursor_ms += piece.duration_ms
        boundary_ms = math.ceil(cursor_ms / frame_ms) * frame_ms
        lines.append(f"file '{_escape(piece.path)}'")
        lines.append(f"duration {(boundary_ms - previous_boundary_ms) / 1000:.3f}")
        previous_boundary_ms = boundary_ms
    lines.append(f"file '{_escape(pieces[-1].path)}'")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def build_track(
    ffmpeg: str,
    *,
    target: Path,
    pieces: list[AvatarPiece],
    work_dir: Path,
) -> Path:
    """Render one avatar's lit/dim sequence into a short RGBA clip.

    The clip is built from stills, so there is no frame rate to honour: ffmpeg keeps each
    image on screen for its declared duration and the overlay samples it at the film's own
    25 fps. Measured 1.5 s for 600 s / 1200 segments.
    """
    for piece in pieces:
        if not piece.path.is_file():
            raise AppError(
                "avatar_image_missing",
                f"头像图不见了：{piece.path.name}",
                hint="重新导入这份 PSD 让模板重新编译。",
                status=409,
            )
        if piece.duration_ms <= 0:
            raise AppError("avatar_piece_empty", "头像轨里有一段时长为 0")

    script = write_concat_list(work_dir / f"{target.stem}.concat.txt", pieces)
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(script),
        "-c:v",
        CODEC,
        "-pix_fmt",
        PIX_FMT,
        "-an",
        str(target),
    ]
    from animatter.media.video import _run as run_ffmpeg  # shared error handling

    log.info(
        "avatar.track",
        pieces=len(pieces),
        total_ms=sum(piece.duration_ms for piece in pieces),
        target=target.name,
    )
    run_ffmpeg(command, timeout=1800)
    script.unlink(missing_ok=True)

    if not target.exists() or target.stat().st_size == 0:
        raise AppError("avatar_track_failed", "头像轨渲染为空文件", hint="检查 ffmpeg 日志。")
    return target


def pieces_of(
    *, lit: list[Path], dim: Path, sequence: list[TrackPiece]
) -> list[AvatarPiece]:
    """Map a lit/dim sequence onto actual image files.

    ``lit`` may hold more than one file: a slot bound to a speaker whose *photo* changes
    (a second guest joining) is not modelled yet, but the signature leaves room for it
    without touching the renderer.
    """
    lit_path = lit[0] if lit else dim
    return [
        AvatarPiece(path=lit_path if piece.lit else dim, duration_ms=piece.duration_ms)
        for piece in sequence
    ]
