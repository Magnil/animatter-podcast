"""Video rendering: filtergraph assembly and the two encode paths (§19.5).

Three measured facts shape this module.

**One ffmpeg, one filtergraph, written to a file.** The graph for a real episode is
big: a 1-hour film with avatars and a box is thousands of characters, and Windows
caps a command line at 32,767. Measured: 12,307 characters runs, 38,187 fails with
``The filename or extension is too long`` — which reads like a path bug and is not.
``-filter_complex_script`` takes the graph as an *input* option (after the ``-i``s);
``-/filter_complex <file>`` also works but only as a *global* option, i.e. before the
first ``-i``. Putting it in the wrong place yields ``Invalid argument``.

**Practically all the time is x264.** Measured per 60 s of output: empty background
10.95 s, background + box + burned subtitles + four avatar tracks 14.19 s. So
compositing is ~3 s and encoding is ~11 s, which means optimising the graph is
pointless and ``preset``/resolution are the only real levers. ``veryfast`` costs 15 %
more than ``ultrafast`` and produces a third of the size.

**The output is deterministic.** Two runs of the same command produced the identical
SHA-256, which is what makes "export twice and compare" a real workflow, and what
lets an export be cached by graph hash the way audio renders are.

Audio is **not** encoded here. The audio render already has its own EDL-hash cache,
progress reporting and worker pool; re-cutting it would burn ten-plus minutes of CPU
for a file that already exists. The last step is a ``-c copy`` mux, measured at 0.24 s
per 60 s.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.media.subtitle_ass import ensure_font as ensure_font_installed

log = get_logger(__name__)

#: 1080p25 is the deliverable. 25 rather than 30 because Chinese broadcast and
#: most podcast video here is 25, and a mismatched rate would judder on a 25 Hz
#: timeline for no benefit.
FPS: Final[int] = 25
CRF: Final[str] = "20"
#: `veryfast` measured 9.42 s per 60 s against `ultrafast`'s 8.06 s, at a third of
#: the file size. The 15 % is not worth tripling the upload.
PRESET: Final[str] = "veryfast"

ProgressFn = Callable[[float], None]


@dataclass(frozen=True)
class VideoResult:
    path: Path
    duration_ms: int
    frames: int


@dataclass(frozen=True)
class Layer:
    """One static RGBA layer to be overlaid at the very top of the stack."""

    path: Path
    x: int = 0
    y: int = 0


def escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use inside a filtergraph option.

    Inside a filter argument ``:`` separates options and ``\\`` escapes, so a Windows
    path ``C:\\x\\sub.ass`` must become ``C\\:/x/sub.ass``. Forward slashes are
    accepted by ffmpeg on every platform and avoid having to escape ``\\`` as well.

    Getting this wrong is invisible until ffmpeg errors with something like
    ``Unable to parse option value`` pointing at a path that looks fine.
    """
    text = path.as_posix()
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_filtergraph(
    *,
    width: int,
    height: int,
    layers: list[Layer],
    subtitle_path: Path | None,
    offset_ms: int = 0,
    out_label: str = "vout",
) -> str:
    """Assemble the video filtergraph.

    ``[0:v]`` is the compiled background. Every other input is a still or a
    pre-generated track overlaid in order; the subtitle burn is last so text is never
    covered by artwork.

    The ``setsar=1`` on each branch is not cosmetic: the compiled PNGs carry no pixel
    aspect metadata, and a mismatch makes the ``overlay`` filter silently scale the
    layer, so the box lands a few pixels off from where the confirm screen showed it.

    ``offset_ms`` exists for the still preview. A still at 6 s is a *frame from 6 s*,
    and ``ass`` decides what to draw from the frame's timestamp — so asking ffmpeg to
    seek instead produces a frame whose PTS is 0 and therefore no subtitle at all.
    Measured: seeking gave 0 differing pixels against a subtitle-free render, while
    shifting PTS by the same offset gave a cue at the expected place. So the offset
    is applied here, to the picture, rather than by seeking the input.
    """
    parts: list[str] = []
    # ``PTS`` is in *timebase units*, not milliseconds: with a 25 fps base,
    # ``PTS+6000/TB`` means 6000 seconds. Writing the offset in seconds is the only
    # spelling that means what it says, and getting it wrong is silent — the frame
    # simply lands outside every cue and no subtitle is drawn.
    shift = f",setpts=PTS+{offset_ms / 1000:.3f}/TB" if offset_ms else ""
    current = "0:v"

    if shift:
        parts.append(f"[0:v]scale={width}:{height},setsar=1{shift}[base0]")
        current = "base0"

    for index, layer in enumerate(layers, start=1):
        label = f"l{index}"
        parts.append(
            f"[{current}]scale={width}:{height},setsar=1[base{index}];"
            f"[{index}:v]scale={width}:{height},setsar=1{shift}[img{index}];"
            f"[base{index}][img{index}]overlay=x={layer.x}:y={layer.y}:format=auto[{label}]"
        )
        current = label

    if subtitle_path is not None:
        parts.append(f"[{current}]ass=filename='{escape_filter_path(subtitle_path)}'[{out_label}]")
    elif current != out_label:
        parts.append(f"[{current}]copy[{out_label}]")

    return ";\n".join(parts)


def _run(command: list[str], *, timeout: int = 7200) -> None:
    """Run ffmpeg, surfacing its stderr when it fails.

    Same UTF-8 posture as ``media/ffmpeg._run``: never ``text=True``, because a CJK
    path raises ``UnicodeDecodeError`` in the reader thread and ``subprocess.run``
    swallows it, leaving ``None`` output and a reported success.
    """
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AppError(
            "ffmpeg_missing",
            f"找不到 {command[0]}",
            hint="安装 ffmpeg 并确保在 PATH 上，或设置 AM_FFMPEG_PATH。",
            status=503,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            "video_render_timeout",
            f"视频编码超时（>{timeout}s）",
            hint="导出这么大的片子本来就慢；先导出较短的片段确认参数没问题。",
            status=504,
        ) from exc

    if completed.returncode != 0:
        raise AppError(
            "video_render_failed",
            "视频渲染失败",
            hint=(completed.stderr or "")[-600:],
            status=500,
        )


def render(
    ffmpeg: str,
    *,
    target: Path,
    duration_ms: int,
    width: int,
    height: int,
    background: Path,
    layers: list[Layer],
    subtitles: Path | None = None,
    preset: str = PRESET,
    crf: str = CRF,
) -> VideoResult:
    """Encode the silent video track.

    Each still is declared with ``-loop 1 -t <seconds>``: without the explicit
    ``-t`` ffmpeg loops forever and the encode never ends. The duration is the
    *rendered* one, so the video is exactly as long as the cut audio.
    """
    if duration_ms <= 0:
        raise AppError("video_duration_invalid", "视频时长为 0", hint="先渲染音频。")
    if not background.is_file():
        raise AppError(
            "template_not_compiled",
            "模板还没编译好",
            hint="在「视频」里重新导入这份 PSD。",
            status=409,
        )
    for layer in layers:
        if not layer.path.is_file():
            raise AppError(
                "template_not_compiled",
                f"模板缺少图层文件 {layer.path.name}",
                hint="重新导入 PSD 让模板重新编译。",
                status=409,
            )
    if subtitles is not None:
        if not subtitles.is_file():
            raise AppError("subtitle_missing", "字幕文件不存在", status=404)
        ensure_font_ok(subtitles)

    target.parent.mkdir(parents=True, exist_ok=True)
    seconds = f"{duration_ms / 1000:.3f}"

    graph = build_filtergraph(
        width=width, height=height, layers=layers, subtitle_path=subtitles
    )
    script = target.with_suffix(target.suffix + ".filtergraph")
    script.write_text(graph, encoding="utf-8")

    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-t",
        seconds,
        "-i",
        str(background),
    ]
    for layer in layers:
        command += ["-loop", "1", "-t", seconds, "-i", str(layer.path)]

    command += [
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
        "-r",
        str(FPS),
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]

    log.info(
        "video.render",
        duration_ms=duration_ms,
        layers=len(layers),
        graph_chars=len(graph),
        subtitles=subtitles is not None,
    )
    _run(command)
    script.unlink(missing_ok=True)

    if not target.exists() or target.stat().st_size == 0:
        raise AppError("video_render_empty", "视频产出为空文件", hint="检查 ffmpeg 日志与磁盘空间。")
    return VideoResult(path=target, duration_ms=duration_ms, frames=duration_ms * FPS // 1000)


def ensure_font_ok(subtitles: Path) -> None:
    """Check the ASS's font family is installed before paying for an encode.

    Reads the family back out of the file rather than trusting the caller: the
    project's saved style is what actually went into the ASS, and a mismatch between
    the two is exactly the case where libass would silently substitute.
    """
    family = _font_family_from_ass(subtitles)
    if family is None:
        return
    ensure_font_installed(SubtitleStyle(font_name=family))


def _font_family_from_ass(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Style: Default,"):
            fields = line.split(",")
            return fields[1].strip() if len(fields) > 1 else None
    return None


def render_still(
    ffmpeg: str,
    *,
    target: Path,
    at_ms: int,
    width: int,
    height: int,
    background: Path,
    layers: list[Layer],
    subtitles: Path | None = None,
) -> VideoResult:
    """Composite one frame at ``at_ms`` for the preview (§19.7).

    A still is ~200 ms end to end, which is what makes v1's preview honest: the user
    drags the playhead and sees the real layout instead of a hand-drawn approximation.
    Real-time playback would need a second render pipeline and a second clock.

    ``-ss`` is deliberately **not** used, for the reason given on
    :func:`build_filtergraph`: seeking resets the frame's PTS to 0, and ``ass`` would
    then draw nothing because no cue covers 0. The offset is applied as a PTS shift on
    an infinitely looping still instead, which costs a few frames of decode and is the
    only version that actually shows the cue.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = build_filtergraph(
        width=width, height=height, layers=layers, subtitle_path=subtitles, offset_ms=max(0, at_ms)
    )
    script = target.with_suffix(target.suffix + ".filtergraph")
    script.write_text(graph, encoding="utf-8")

    command = [ffmpeg, "-nostdin", "-y", "-loglevel", "error"]
    if subtitles is not None:
        ensure_font_ok(subtitles)
    for path in [background, *(layer.path for layer in layers)]:
        command += ["-loop", "1", "-i", str(path)]

    command += [
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-c:v",
        "png",
        str(target),
    ]

    _run(command, timeout=120)
    script.unlink(missing_ok=True)
    if not target.exists() or target.stat().st_size == 0:
        raise AppError("preview_failed", "静帧预览生成失败", hint="检查 ffmpeg 日志。")
    return VideoResult(path=target, duration_ms=0, frames=1)


def mux(
    ffmpeg: str,
    *,
    video: Path,
    audio: Path,
    target: Path,
) -> VideoResult:
    """Stick the rendered audio onto the silent video without re-encoding either.

    ``-c copy`` measured 0.24 s per 60 s — three orders of magnitude cheaper than
    encoding audio here, and it guarantees the film's sound is bit-identical to the
    audio the user already exported and approved.

    ``-shortest`` is deliberately **not** used: the two tracks are both derived from
    the same EDL, and if they ever disagree slightly, silently truncating one hides a
    real bug behind a plausible-looking file.
    """
    if not video.is_file():
        raise AppError("video_missing", "视频轨还没渲染好", status=404)
    if not audio.is_file():
        raise AppError(
            "audio_missing",
            "成片音频还没渲染好",
            hint="先导出成片音频，再导出视频。",
            status=404,
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    _run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    if not target.exists() or target.stat().st_size == 0:
        raise AppError("mux_failed", "混流产出为空文件", hint="检查 ffmpeg 日志与磁盘空间。")

    duration = _probe_duration_ms(ffmpeg, target)
    return VideoResult(path=target, duration_ms=duration, frames=0)


def _probe_duration_ms(ffmpeg: str, path: Path) -> int:
    """Read the muxed file's duration, so the caller never has to guess it."""
    try:
        completed = subprocess.run(
            [ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    text = (completed.stdout or "").strip()
    try:
        return round(float(text) * 1000)
    except ValueError:
        return 0
