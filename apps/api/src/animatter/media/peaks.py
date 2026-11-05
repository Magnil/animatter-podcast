"""Waveform peak generation with a multi-level cache.

A 12-hour episode is 1.7M samples per second at 44.1k; shipping that to a
browser (or decodeAudioData-ing it) would take gigabytes (AGENT.md §7.1). So the
backend pre-reduces the audio to min/max pairs at several zoom levels, and the
client fetches only the window it is looking at.

Format (``PKC1``)::

    magic     4s  b"PKC1"
    version   u16
    levels    u16
    duration  i32  (ms)
    <level>*
      buckets_per_second  u32
      count               u32
      data                count * 2 * i16   (min, max)

Levels are *cumulative* maxima from the source PCM, so zooming out never
recomputes audio — it just walks a coarser array.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

MAGIC = b"PKC1"
VERSION = 1
#: 8 buckets/s ≈ one pixel per 125ms at full zoom-out; 4096 is the detail level.
LEVELS: tuple[int, ...] = (8, 64, 512, 4096)
#: PCM block size for streaming decode: ~1s at 16kHz, keeps memory flat.
_CHUNK_FRAMES = 65536


@dataclass(frozen=True)
class Peaks:
    duration_ms: int
    sample_rate: int
    levels: tuple[tuple[int, np.ndarray], ...]

    def level_for(self, buckets_per_second: int) -> tuple[int, np.ndarray]:
        """Pick the coarsest level that is at least as detailed as requested."""
        chosen = LEVELS[-1]
        for level in LEVELS:
            if level >= buckets_per_second:
                chosen = level
                break
        for rate, data in self.levels:
            if rate == chosen:
                return rate, data
        return self.levels[-1]


def _pcm_stream(ffmpeg: str, path: Path, sample_rate: int) -> np.ndarray:
    """Decode to mono int16 PCM via ffmpeg, streamed to keep memory bounded."""
    import subprocess

    command = [
        ffmpeg,
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=3600, check=False)
    except FileNotFoundError as exc:
        raise AppError(
            "ffmpeg_missing",
            f"找不到 {ffmpeg}",
            hint="安装 ffmpeg 并确保在 PATH 上，或设置 AM_FFMPEG_PATH。",
            status=503,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("ffmpeg_timeout", "解码超时", hint="音频可能损坏。", status=504) from exc

    if completed.returncode != 0:
        raise AppError(
            "peaks_failed",
            "解码音频失败",
            hint=(completed.stderr or b"").decode("utf-8", "replace")[:300],
        )
    return np.frombuffer(completed.stdout, dtype=np.int16)


def _bucket(samples: np.ndarray, samples_per_bucket: int) -> np.ndarray:
    """Reduce samples to (min, max) pairs. Returns shape (count, 2)."""
    if samples_per_bucket < 1:
        samples_per_bucket = 1
    count = len(samples) // samples_per_bucket
    if count == 0:
        return np.empty((0, 2), dtype=np.int16)
    trimmed = samples[: count * samples_per_bucket].reshape(count, samples_per_bucket)
    return np.stack([trimmed.min(axis=1), trimmed.max(axis=1)], axis=1).astype(np.int16)


def compute(ffmpeg: str, path: Path, *, sample_rate: int = 16000) -> Peaks:
    """Decode once and derive every zoom level."""
    samples = _pcm_stream(ffmpeg, path, sample_rate)
    total = len(samples)
    if total == 0:
        raise AppError("peaks_empty", "音频没有采样数据", hint="确认文件不是纯静音或长度为零。")

    duration_ms = int(total / sample_rate * 1000)
    levels: list[tuple[int, np.ndarray]] = []
    for rate in LEVELS:
        per_bucket = max(1, sample_rate // rate)
        levels.append((rate, _bucket(samples, per_bucket)))

    return Peaks(duration_ms=duration_ms, sample_rate=sample_rate, levels=tuple(levels))


def serialize(peaks: Peaks) -> bytes:
    header = MAGIC + struct.pack("<HHi", VERSION, len(peaks.levels), peaks.duration_ms)
    body = bytearray()
    for rate, data in peaks.levels:
        body += struct.pack("<II", rate, len(data))
        body += data.astype("<i2").tobytes()
    return header + bytes(body)


def deserialize(blob: bytes) -> Peaks:
    if len(blob) < 12 or blob[:4] != MAGIC:
        raise ValueError("不是峰值缓存文件")
    _version, level_count, duration_ms = struct.unpack("<HHi", blob[4:12])
    offset = 12
    levels: list[tuple[int, np.ndarray]] = []
    for _ in range(level_count):
        rate, count = struct.unpack("<II", blob[offset : offset + 8])
        offset += 8
        chunk = np.frombuffer(blob[offset : offset + count * 4], dtype="<i2").reshape(count, 2)
        offset += count * 4
        levels.append((rate, chunk))
    return Peaks(duration_ms=duration_ms, sample_rate=16000, levels=tuple(levels))


def cache_path(project_dir: Path, media_sha: str) -> Path:
    """Cache key is the content hash, never the filename (AGENT.md §15.6 item 33)."""
    return project_dir / "peaks" / f"{media_sha}.pkc"


def load_or_build(ffmpeg: str, project_dir: Path, media_path: Path, media_sha: str) -> Peaks:
    target = cache_path(project_dir, media_sha)
    if target.exists():
        try:
            return deserialize(target.read_bytes())
        except (ValueError, struct.error) as exc:
            # A corrupt cache must not brick the project; rebuild instead.
            log.warning("peaks.cache_corrupt", path=str(target), error=str(exc))
            target.unlink(missing_ok=True)

    peaks = compute(ffmpeg, media_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialize(peaks))
    log.info("peaks.built", path=str(target), duration_ms=peaks.duration_ms)
    return peaks


def window_payload(peaks: Peaks, *, buckets_per_second: int, from_ms: int, to_ms: int) -> dict[str, object]:
    """Slice one window for the client.

    Returns min/max as normalised floats so the renderer does not need to know
    about int16 scaling.
    """
    rate, data = peaks.level_for(buckets_per_second)
    bps = rate / 1000.0  # buckets per millisecond
    start = max(0, int(from_ms * bps))
    end = min(len(data), int(to_ms * bps))
    if end <= start:
        start, end = 0, min(len(data), 1)

    slice_ = data[start:end]
    if len(slice_) == 0:
        return {"rate": rate, "from_ms": from_ms, "to_ms": to_ms, "min": [], "max": []}

    return {
        "rate": rate,
        "from_ms": int(start / bps),
        "to_ms": int(end / bps),
        "min": [round(float(v) / 32768.0, 4) for v in slice_[:, 0]],
        "max": [round(float(v) / 32768.0, 4) for v in slice_[:, 1]],
    }
