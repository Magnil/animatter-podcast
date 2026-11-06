"""The silence map for one episode, shared by every caller that needs it.

Two features ask "where are the silences": boundary snapping (``clipping``) and
one-click cleanup. They must agree, and they must agree on *parameters* — the
detector's noise floor and minimum duration belong to the map, not to the cut
decision, or moving the cleanup slider would silently move every snapped edge.

The cache in :mod:`animatter.media.silences` is keyed by content hash plus those
parameters. Until this module existed, ``clipping.load_silences`` bypassed it and
called ``ffmpeg silencedetect`` on the whole file on **every** ``build_edl`` —
which happens on every edit. Measured on a 105-minute episode: 9.1 s per call.
One-click cleanup had already solved this; the editor simply never got the same
treatment, so a single drag of a sentence boundary re-decoded nearly two hours
of audio before the UI could repaint.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import SilenceSpan
from animatter.log import get_logger
from animatter.media import silences as silence_cache
from animatter.models import Media, TranscriptVersion
from animatter.services import projects as project_service
from animatter.services import settings_store

log = get_logger(__name__)


def noise_db(session: Session) -> float:
    return float(settings_store.load(session)["silence_noise_db"])


def min_duration_ms(session: Session) -> int:
    return int(settings_store.load(session)["silence_min_duration_ms"])


def for_media(
    session: Session,
    settings: Settings,
    version: TranscriptVersion,
    media: Media,
) -> list[SilenceSpan]:
    """Detected silences for this media, from cache when the key matches."""
    project_dir = project_service.project_root(settings, version.project_id)
    cached = silence_cache.load_or_build(
        settings.ffmpeg_path,
        project_dir=project_dir,
        media_path=Path(media.path),
        media_sha=media.sha256,
        duration_ms=media.duration_ms,
        noise_db=noise_db(session),
        min_duration_s=min_duration_ms(session) / 1000,
    )
    return [SilenceSpan(start_ms=start, end_ms=end) for start, end in cached.spans]
