"""ffmpeg/ffprobe invocation: probing, ASR pre-transcode, and silence detection.

All commands are argument lists (never ``shell=True``), because media paths on
this platform routinely contain spaces and CJK characters.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError

#: ASR input is forced to this profile: it fits a 12h episode under the 1GB
#: temporary-storage limit (AGENT.md §6.0).
ASR_SAMPLE_RATE: Final[int] = 16000
ASR_CHANNELS: Final[int] = 1
ASR_BITRATE: Final[str] = "64k"

#: Bytes per second of the ASR transcode, used to predict the upload size.
_ASR_BYTES_PER_SECOND: Final[int] = 8_000

_PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    sample_rate: int
    channels: int
    codec: str
    format_name: str

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "codec": self.codec,
            "format_name": self.format_name,
        }


def _run(cmd: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AppError(
            "ffmpeg_missing",
            f"找不到 {cmd[0]}",
            hint="安装 ffmpeg 并确保在 PATH 上，或设置 AM_FFMPEG_PATH / AM_FFPROBE_PATH。",
            status=503,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            "ffmpeg_timeout",
            f"{Path(cmd[0]).name} 执行超时（>{timeout}s）",
            hint="音频可能损坏或磁盘过慢；先用播放器确认文件能正常打开。",
            status=504,
        ) from exc


def probe(ffprobe: str, path: Path) -> MediaInfo:
    """Read container/stream facts. Raises AppError when the file is unusable."""
    completed = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-select_streams",
            "a:0",
            str(path),
        ],
        timeout=120,
    )
    if completed.returncode != 0:
        raise AppError(
            "media_unreadable",
            "无法解析这个音频文件",
            hint=f"ffprobe 报错：{completed.stderr.strip()[:300]}",
        )

    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise AppError("media_no_audio", "文件里没有音轨", hint="确认选择的是音频或带音轨的视频文件。")

    stream = streams[0]
    duration = float(payload.get("format", {}).get("duration") or stream.get("duration") or 0)
    return MediaInfo(
        duration_ms=round(duration * 1000),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        codec=str(stream.get("codec_name") or ""),
        format_name=str(payload.get("format", {}).get("format_name") or ""),
    )


def estimate_asr_size_bytes(duration_ms: int) -> int:
    return int(duration_ms / 1000) * _ASR_BYTES_PER_SECOND


def transcode_for_asr(ffmpeg: str, src: Path, dst: Path) -> Path:
    """Produce the mono 16 kHz mp3 that gets uploaded to Bailian.

    Re-encoded rather than stream-copied: a 12h 44.1k stereo WAV is ~7.6GB and
    would be rejected by the 1GB temporary-storage limit.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            str(ASR_CHANNELS),
            "-ar",
            str(ASR_SAMPLE_RATE),
            "-c:a",
            "libmp3lame",
            "-b:a",
            ASR_BITRATE,
            str(dst),
        ]
    )
    if completed.returncode != 0 or not dst.exists():
        raise AppError(
            "asr_transcode_failed",
            "转码为识别输入失败",
            hint=f"ffmpeg 报错：{completed.stderr.strip()[:300]}",
        )
    return dst


def extract_segment(ffmpeg: str, src: Path, dst: Path, start_ms: int, end_ms: int) -> Path:
    """Cut ``[start_ms, end_ms)`` out of ``src`` for chunked transcription."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-to",
            f"{end_ms / 1000:.3f}",
            "-i",
            str(src),
            "-vn",
            "-ac",
            str(ASR_CHANNELS),
            "-ar",
            str(ASR_SAMPLE_RATE),
            "-c:a",
            "libmp3lame",
            "-b:a",
            ASR_BITRATE,
            str(dst),
        ]
    )
    if completed.returncode != 0 or not dst.exists():
        raise AppError(
            "segment_extract_failed",
            f"切分音频失败（{start_ms}–{end_ms}ms）",
            hint=f"ffmpeg 报错：{completed.stderr.strip()[:300]}",
        )
    return dst


@dataclass(frozen=True)
class SilenceSpan:
    start_ms: int
    end_ms: int

    @property
    def midpoint_ms(self) -> int:
        return (self.start_ms + self.end_ms) // 2

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence(
    ffmpeg: str, path: Path, *, noise_db: float = -35.0, min_duration_s: float = 0.4
) -> list[SilenceSpan]:
    """Return silent spans via ffmpeg's ``silencedetect`` filter.

    A missing binary raises ``AppError(ffmpeg_missing)``; a *non-zero exit* on a
    readable file returns an empty list, because the caller can still fall back
    to a hard cut and record ``seam_quality='forced'`` rather than aborting.
    """
    completed = _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration_s}",
            "-f",
            "null",
            "-",
        ],
        timeout=1800,
    )
    if completed.returncode != 0:
        return []

    spans: list[SilenceSpan] = []
    pending_start: int | None = None
    for line in completed.stderr.splitlines():
        if (match := _SILENCE_START.search(line)) is not None:
            pending_start = int(float(match.group(1)) * 1000)
        elif (match := _SILENCE_END.search(line)) is not None and pending_start is not None:
            spans.append(SilenceSpan(pending_start, int(float(match.group(1)) * 1000)))
            pending_start = None
    return spans


def choose_chunk_points(
    duration_ms: int,
    silences: list[SilenceSpan],
    *,
    target_ms: int,
    search_window_ms: int = 5 * 60 * 1000,
) -> tuple[list[int], str]:
    """Pick cut points near each ``target_ms`` multiple, preferring silence midpoints.

    Returns ``(points, quality)`` where quality is ``'silence'`` when every cut
    landed in a silence and ``'forced'`` when at least one had to be hard-cut.
    """
    if duration_ms <= target_ms:
        return [], "silence"

    points: list[int] = []
    forced = False
    target = target_ms
    while target < duration_ms:
        candidates = [
            silence for silence in silences if abs(silence.midpoint_ms - target) <= search_window_ms
        ]
        if candidates:
            best = min(candidates, key=lambda s: abs(s.midpoint_ms - target))
            points.append(best.midpoint_ms)
        else:
            points.append(target)
            forced = True
        target += target_ms

    return points, ("forced" if forced else "silence")


def build_segments(duration_ms: int, points: list[int]) -> list[tuple[int, int]]:
    """Turn cut points into ``[start, end)`` segments covering the whole file."""
    boundaries = [0, *sorted(p for p in points if 0 < p < duration_ms), duration_ms]
    return [
        (boundaries[i], boundaries[i + 1])
        for i in range(len(boundaries) - 1)
        if boundaries[i] < boundaries[i + 1]
    ]
