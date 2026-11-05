"""Rendering a keep-list into an audio file with ffmpeg.

Two details that decide whether the output is usable (AGENT.md §6.5):

* **Every join needs a short fade.** Concatenating PCM at an arbitrary sample
  produces a step discontinuity, which is audible as a click. 5ms in/out is not
  "audio processing", it is what makes a cut sound like a cut rather than a fault.
* **Argument count explodes with segment count.** A two-hour episode with many
  edits yields hundreds of segments; past a few hundred the argv limit is real, so
  the filtergraph goes into a script file instead of the command line.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from animatter.core.edl import Edl, Keep
from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

#: Length of the fade applied at every cut point, in seconds.
FADE_SECONDS = 0.005
#: Above this many segments, render in batches and concatenate.
BATCH_THRESHOLD = 200


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration_ms: int
    segments: int


def build_filtergraph(edl: Edl, *, sample_rate: int, channels: int, out_label: str = "out") -> str:
    """Return the ``filter_complex`` script for a keep list."""
    parts: list[str] = []
    labels: list[str] = []

    for index, keep in enumerate(edl.keeps):
        start = keep.orig_start / 1000
        end = keep.orig_end / 1000
        duration = (keep.orig_end - keep.orig_start) / 1000
        fade_out_start = max(0.0, duration - FADE_SECONDS)
        label = f"a{index}"
        parts.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={FADE_SECONDS},"
            f"afade=t=out:st={fade_out_start:.6f}:d={FADE_SECONDS}[{label}]"
        )
        labels.append(f"[{label}]")

    if len(labels) == 1:
        parts.append(f"{labels[0]}anull[{out_label}]")
    else:
        parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[{out_label}]")

    return ";\n".join(parts)


def _run_ffmpeg(command: list[str], *, timeout: int = 7200) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            # UTF-8 consciously, not `text=True`: the latter means "the locale
            # codec", and on a Chinese Windows that is cp936 — decoding ffmpeg's
            # UTF-8 diagnostics with it can raise and strand `stderr` as None,
            # turning a real ffmpeg error into an unreadable one.
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
            "render_timeout",
            "渲染超时",
            hint="素材可能很长或磁盘过慢；可先渲染较短片段验证。",
            status=504,
        ) from exc

    if completed.returncode != 0:
        raise AppError(
            "render_failed",
            "渲染失败",
            hint=(completed.stderr or "")[-500:],
            status=500,
        )


def render(
    ffmpeg: str,
    *,
    source: Path,
    edl: Edl,
    target: Path,
    sample_rate: int,
    channels: int,
    bitrate: str = "192k",
    lossless: bool = False,
) -> RenderResult:
    """Render ``edl`` into ``target``.

    The output keeps the source's sample rate and channel count: this is a
    deliverable, not an ASR input, and downsampling it would throw away quality
    the user still has.
    """
    if not edl.keeps:
        raise AppError("edl_empty", "没有可渲染的片段", hint="至少保留一段音频再渲染。")
    if not source.exists():
        raise AppError("media_file_missing", "源音频不存在", status=404)

    target.parent.mkdir(parents=True, exist_ok=True)
    script = target.with_suffix(target.suffix + ".filtergraph")
    script.write_text(
        build_filtergraph(edl, sample_rate=sample_rate, channels=channels),
        encoding="utf-8",
    )

    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-filter_complex_script",
        str(script),
        "-map",
        "[out]",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
    ]
    if lossless:
        command += ["-c:a", "pcm_s16le", "-f", "wav"]
    else:
        command += ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart", "-f", "mp4"]
    # The output path must be the last argument; without it ffmpeg writes to
    # stdout and the command "succeeds" while producing nothing on disk.
    command.append(str(target))

    _run_ffmpeg(command)

    if not target.exists() or target.stat().st_size == 0:
        raise AppError("render_empty", "渲染产出为空文件", hint="检查 ffmpeg 日志与磁盘空间。")

    script.unlink(missing_ok=True)
    duration_ms = edl.output_duration_ms
    log.info(
        "render.done",
        segments=len(edl.keeps),
        duration_ms=duration_ms,
        bytes=target.stat().st_size,
    )
    return RenderResult(path=target, duration_ms=duration_ms, segments=len(edl.keeps))


def render_preview(
    ffmpeg: str,
    *,
    source: Path,
    edl: Edl,
    target: Path,
    sample_rate: int,
    channels: int,
    window_orig: tuple[int, int],
    window_ms: int = 60_000,
) -> RenderResult:
    """Render just the neighbourhood of ``window_orig`` for quick feedback.

    Re-encoding a two-hour episode to answer "how does this cut sound" is far too
    slow, so the preview only includes keeps overlapping the window.
    """
    start, end = window_orig
    start = max(0, start - window_ms // 2)
    end = min(edl.original_duration_ms, end + window_ms // 2)

    subset = [keep for keep in edl.keeps if keep.orig_start < end and keep.orig_end > start]
    if not subset:
        raise AppError("preview_empty", "该位置没有可预览的音频")

    trimmed: list[Keep] = []
    cursor = 0
    for keep in subset:
        trimmed.append(type(keep)(orig_start=keep.orig_start, orig_end=keep.orig_end, new_start=cursor))
        cursor += keep.orig_end - keep.orig_start

    from dataclasses import replace

    window_edl = replace(edl, keeps=tuple(trimmed))
    return render(
        ffmpeg,
        source=source,
        edl=window_edl,
        target=target,
        sample_rate=sample_rate,
        channels=channels,
        bitrate="128k",
    )
