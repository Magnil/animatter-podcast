"""Cached silence map for one media file.

``ffmpeg silencedetect`` decodes the whole file. Until now it ran only when a
deletion needed snapping, and only near the cut points, so the cost was paid
once per render. One-click cleanup changes that: it needs the **whole** silence
list on every plan, and a plan is recomputed as the user toggles tick boxes.

Caching therefore stops being an optimisation and becomes the thing that makes
the feature usable on a 3-hour episode. The cache key is the *content hash plus
the detector parameters*, never the filename: changing the threshold must
produce a different key, or the user would move a slider and see nothing change.
This mirrors ``media/peaks.py`` on purpose — same idea, same shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from animatter.log import get_logger
from animatter.media import ffmpeg

log = get_logger(__name__)

#: Bump when the on-disk shape changes so old cache files are ignored.
CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class SilenceMap:
    duration_ms: int
    noise_db: float
    min_duration_s: float
    spans: tuple[tuple[int, int], ...]

    def as_ranges(self) -> list[tuple[int, int]]:
        return list(self.spans)


def cache_path(project_dir: Path, media_sha: str, *, noise_db: float, min_duration_s: float) -> Path:
    """One file per (content, parameters) pair."""
    stamp = f"v{CACHE_VERSION}-{noise_db:g}db-{min_duration_s:g}s"
    return project_dir / "silences" / f"{media_sha}.{stamp}.json"


def _read(path: Path) -> SilenceMap | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SilenceMap(
            duration_ms=int(payload["duration_ms"]),
            noise_db=float(payload["noise_db"]),
            min_duration_s=float(payload["min_duration_s"]),
            spans=tuple((int(a), int(b)) for a, b in payload["spans"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A corrupt cache must not brick the project; rebuild instead.
        log.warning("silences.cache_corrupt", path=str(path), error=str(exc))
        path.unlink(missing_ok=True)
        return None


def _write(path: Path, value: SilenceMap) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "duration_ms": value.duration_ms,
                "noise_db": value.noise_db,
                "min_duration_s": value.min_duration_s,
                "spans": [list(span) for span in value.spans],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_or_build(
    ffmpeg_path: str,
    *,
    project_dir: Path,
    media_path: Path,
    media_sha: str,
    duration_ms: int,
    noise_db: float = -35.0,
    min_duration_s: float = 0.3,
) -> SilenceMap:
    """Detected silences for ``media_path``, from cache when the key matches."""
    target = cache_path(project_dir, media_sha, noise_db=noise_db, min_duration_s=min_duration_s)
    if target.exists():
        cached = _read(target)
        if cached is not None:
            return cached

    raw = ffmpeg.detect_silence(
        ffmpeg_path, media_path, noise_db=noise_db, min_duration_s=min_duration_s
    )
    value = SilenceMap(
        duration_ms=duration_ms,
        noise_db=noise_db,
        min_duration_s=min_duration_s,
        spans=tuple((span.start_ms, span.end_ms) for span in raw),
    )
    _write(target, value)
    log.info(
        "silences.built",
        path=str(target),
        spans=len(value.spans),
        noise_db=noise_db,
        min_duration_s=min_duration_s,
    )
    return value


def invalidate(project_dir: Path, media_sha: str) -> int:
    """Drop every cached parameter set for one media file. Returns count removed."""
    folder = project_dir / "silences"
    if not folder.exists():
        return 0
    removed = 0
    for path in folder.glob(f"{media_sha}.*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed
