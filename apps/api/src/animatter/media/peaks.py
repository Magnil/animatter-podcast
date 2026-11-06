"""Waveform envelope: one min/max pair per bucket, for the whole file.

**Why a single level, whole-file, instead of a windowed multi-level cache.**

The previous design shipped only the visible window, on the theory that a 12h
episode at 4096 buckets/s would be 220M buckets and no browser could hold it.
That was true for 12 hours and irrelevant for the episodes this app actually
gets: a 105-minute podcast at the finest useful resolution is 2M buckets, which
as signed bytes is **4 MB**. Meanwhile the windowed design was costing a network
round trip *per wheel notch* — measured at 121–152 ms each on a real episode —
because zooming changed the window and the window was the request key. The
zoom felt laggy for the same reason a page feels laggy when every scroll bar
drag hits the server.

So: build the envelope once, ship it once, keep it in memory, and let zooming
be pure arithmetic. That is what makes the timeline feel instant; the drawing
was never the problem (measured: 978 canvas columns ≈ 1.5 ms).

Resolution policy: ``MAX_BUCKETS`` caps the array, so bucket duration scales
with file length. For a 105-minute file that is ~3.2 ms per bucket, i.e. about
one bucket per pixel at the minimum window of 3 s. Shorter files keep full
sample resolution — the cap only ever coarsens.

Format (``PKE1``)::

    magic    4s  b"PKE1"
    version  u16
    count    u32   number of buckets
    duration i32   (ms)
    rate     u32   (source sample rate)
    min      count * i8   (quantised -127..127)
    max      count * i8

Int8 rather than float32 is deliberate: a waveform envelope is a visual shape,
not audio. 127 levels of amplitude are indistinguishable once a column is two
pixels tall, and it turns a 16 MB payload into 4 MB.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

MAGIC = b"PKE1"
VERSION = 1
#: Ceiling on envelope buckets. At 2M the array is 4 MB (2 × int8), which is
#: about one bucket per pixel at the minimum 3-second window on a 105-minute
#: file. Raising it costs memory on both sides for no visible gain.
MAX_BUCKETS = 2_000_000
#: PCM block size for streaming decode: ~4s at 16kHz, keeps memory flat.
_CHUNK_FRAMES = 65536
#: Decode rate. The envelope is a shape; 16 kHz is plenty and keeps the
#: intermediate PCM array small.
ENVELOPE_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Peaks:
    """The whole file's envelope, already reduced to display resolution."""

    duration_ms: int
    sample_rate: int
    #: Quantised extrema, one entry per bucket.
    min: np.ndarray
    max: np.ndarray

    @property
    def count(self) -> int:
        return int(self.min.shape[0])

    @property
    def bucket_ms(self) -> float:
        """Duration of one bucket in milliseconds."""
        if self.count == 0 or self.duration_ms <= 0:
            return 0.0
        return self.duration_ms / self.count


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
    """Reduce samples to (min, max) pairs. Returns shape (count, 2), int16."""
    if samples_per_bucket < 1:
        samples_per_bucket = 1
    count = len(samples) // samples_per_bucket
    if count == 0:
        return np.empty((0, 2), dtype=np.int16)
    trimmed = samples[: count * samples_per_bucket].reshape(count, samples_per_bucket)
    return np.stack([trimmed.min(axis=1), trimmed.max(axis=1)], axis=1).astype(np.int16)


def _quantise(values: np.ndarray) -> np.ndarray:
    """int16 PCM to signed bytes, clamping anything that overshoots ±127."""
    scaled = np.rint(values.astype(np.float32) / 257.0)  # 32768/127.5 ≈ 257
    return np.clip(scaled, -127, 127).astype(np.int8)


def compute(
    ffmpeg: str, path: Path, *, sample_rate: int = ENVELOPE_SAMPLE_RATE
) -> Peaks:
    """Decode once, reduce to a single display-resolution envelope."""
    samples = _pcm_stream(ffmpeg, path, sample_rate)
    total = len(samples)
    if total == 0:
        raise AppError("peaks_empty", "音频没有采样数据", hint="确认文件不是纯静音或长度为零。")

    duration_ms = int(total / sample_rate * 1000)
    # Short files keep one bucket per sample; long ones are capped and coarsen.
    buckets = min(MAX_BUCKETS, total)
    per_bucket = max(1, total // max(1, buckets))
    pairs = _bucket(samples, per_bucket)

    return Peaks(
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        min=_quantise(pairs[:, 0]),
        max=_quantise(pairs[:, 1]),
    )


def serialize(peaks: Peaks) -> bytes:
    header = MAGIC + struct.pack("<Hiii", VERSION, peaks.count, peaks.duration_ms, peaks.sample_rate)
    return header + peaks.min.astype("<i1").tobytes() + peaks.max.astype("<i1").tobytes()


def deserialize(blob: bytes) -> Peaks:
    if len(blob) < 18 or blob[:4] != MAGIC:
        raise ValueError("不是峰值缓存文件")
    _version, count, duration_ms, sample_rate = struct.unpack("<Hiii", blob[4:18])
    expected = 18 + count * 2
    if len(blob) < expected:
        raise ValueError(f"峰值缓存被截断：期望 {expected} 字节，实际 {len(blob)}")
    mins = np.frombuffer(blob[18 : 18 + count], dtype="<i1").copy()
    maxs = np.frombuffer(blob[18 + count : expected], dtype="<i1").copy()
    return Peaks(duration_ms=duration_ms, sample_rate=sample_rate, min=mins, max=maxs)


def cache_path(project_dir: Path, media_sha: str) -> Path:
    """Cache key is the content hash, never the filename (AGENT.md §15.6 item 33)."""
    return project_dir / "peaks" / f"{media_sha}.pke"


#: Small LRU over deserialised envelopes.
#:
#: Without this, every waveform request re-read and re-parsed the cache file:
#: measured at 99 ms for a 105-minute episode, paid on every scroll. The array
#: itself is 4 MB, so a handful of entries is a rounding error next to the
#: 143 MB the old multi-level cache held on disk.
_MEMORY: dict[str, Peaks] = {}
_MEMORY_LIMIT = 4


def _remember(key: str, peaks: Peaks) -> Peaks:
    _MEMORY.pop(key, None)
    _MEMORY[key] = peaks
    while len(_MEMORY) > _MEMORY_LIMIT:
        oldest = next(iter(_MEMORY))
        del _MEMORY[oldest]
    return peaks


def forget(media_sha: str) -> None:
    """Drop any cached envelope for this media (re-import, deletion)."""
    for key in [k for k in _MEMORY if k.startswith(f"{media_sha}:")]:
        del _MEMORY[key]


def forget_legacy_cache(project_dir: Path, media_sha: str) -> None:
    """Delete the old multi-level ``.pkc`` file for this media, if present.

    The format changed from four interleaved int16 levels to one int8 envelope.
    Leaving the 143 MB file behind costs disk on every project for a file the
    code can no longer read, and it is not obviously garbage: it lives next to
    the new one and shares its name stem.
    """
    legacy = project_dir / "peaks" / f"{media_sha}.pkc"
    if not legacy.exists():
        return
    try:
        legacy.unlink()
        log.info("peaks.legacy_removed", path=str(legacy))
    except OSError as exc:  # pragma: no cover - disk permission problems only
        log.warning("peaks.legacy_unlink_failed", path=str(legacy), error=str(exc))


def load_or_build(ffmpeg: str, project_dir: Path, media_path: Path, media_sha: str) -> Peaks:
    target = cache_path(project_dir, media_sha)
    key = f"{media_sha}:{target}"
    cached = _MEMORY.get(key)
    if cached is not None:
        return cached

    # Before the load, not only after a build: a project whose envelope was
    # already built by an earlier run never reaches the build path, and its
    # 143 MB predecessor would otherwise sit there forever.
    forget_legacy_cache(project_dir, media_sha)

    if target.exists():
        try:
            return _remember(key, deserialize(target.read_bytes()))
        except (ValueError, struct.error) as exc:
            # A corrupt cache must not brick the project; rebuild instead.
            log.warning("peaks.cache_corrupt", path=str(target), error=str(exc))
            target.unlink(missing_ok=True)

    peaks = compute(ffmpeg, media_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialize(peaks))
    log.info("peaks.built", path=str(target), duration_ms=peaks.duration_ms, buckets=peaks.count)
    return _remember(key, peaks)


def window_payload(peaks: Peaks, *, buckets_per_second: int, from_ms: int, to_ms: int) -> dict[str, object]:
    """Slice one window as JSON floats.

    Kept for ``GET /waveform``, which the editor no longer calls — the editor
    takes the whole envelope and zooms locally. It stays because it is the only
    way to inspect a stretch without shipping 4 MB, and because out-of-tree
    scripts use it.
    """
    if peaks.count == 0 or peaks.duration_ms <= 0:
        return {"rate": buckets_per_second, "from_ms": from_ms, "to_ms": to_ms, "min": [], "max": []}

    bucket_ms = peaks.bucket_ms
    start = max(0, min(peaks.count, int(from_ms / bucket_ms)))
    end = max(start, min(peaks.count, int(to_ms / bucket_ms)))
    if end <= start:
        start, end = 0, min(peaks.count, 1)

    # Decimate to the requested rate so the JSON stays in the same ballpark as
    # the old per-level responses; the client only ever wanted one bucket per
    # couple of pixels.
    wanted = max(1, int((end - start) * bucket_ms / 1000 * buckets_per_second))
    factor = max(1, (end - start) // wanted)
    usable = start + ((end - start) // factor) * factor
    mins = peaks.min[start:usable].reshape(-1, factor).min(axis=1)
    maxs = peaks.max[start:usable].reshape(-1, factor).max(axis=1)

    return {
        "rate": 1000.0 / (bucket_ms * factor),
        "from_ms": int(start * bucket_ms),
        "to_ms": int(usable * bucket_ms),
        "min": [round(float(v) / 127.0, 4) for v in mins],
        "max": [round(float(v) / 127.0, 4) for v in maxs],
    }
