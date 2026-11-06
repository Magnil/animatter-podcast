"""Audacity label export: the word timeline as a label track.

Three decisions worth stating, because each is easy to reverse later by accident:

**Original timeline, not the rendered one.** Subtitles are mapped through the
TimeMap so they line up with the exported audio. Labels are the opposite: the
user imports the *original* audio into Audacity, imports these labels onto it,
and makes the cuts there. Mapping them would offset every label by the amount
already deleted.

**No export row, no file on disk.** This is a mid-edit hand-off, so the endpoint
builds bytes and returns them. Writing a file first would mean a download that
can fail *after* it wrote state.

**Nothing requires existing deletions.** Being able to hand the word timeline to
another tool matters most on a fresh transcript — which is exactly the case that
was blocked, because 导出成片音频 is disabled until something is cut.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core import labels as label_core
from animatter.core.cleanup import find_silence_cuts
from animatter.core.fillers import parse_word_list
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Deletion as DeletionRow
from animatter.models import Media, Token, TranscriptVersion
from animatter.services import cleanup as cleanup_service
from animatter.services import projects as project_service
from animatter.services import settings_store

log = get_logger(__name__)


def _version_or_raise(session: Session, version_id: str) -> TranscriptVersion:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)
    return version


def _deleted_indices(session: Session, version_id: str) -> set[int]:
    """Token indices any not-undone deletion covers.

    Duplicated from ``services.cleanup`` rather than importing its private
    helper: reaching across for it would make a rename there break the export.
    """
    gone: set[int] = set()
    rows = session.scalars(
        select(DeletionRow).where(DeletionRow.version_id == version_id, DeletionRow.removed.is_(False))
    ).all()
    for row in rows:
        try:
            gone.update(int(value) for value in json.loads(row.token_ids_json or "[]"))
        except (ValueError, TypeError):
            continue
    return gone


def filename_for(session: Session, version_id: str) -> str:
    """``漫物质_audacity_labels.txt`` — the project name, not ``original``.

    The reference script named the file after the audio, which for us is always
    ``original.mp3``; the project name is what the user recognises in a download
    folder holding six episodes.
    """
    version = _version_or_raise(session, version_id)
    project = project_service.get_project(session, version.project_id)
    return label_core.suggest_filename(project.name, version=version.version)


def build(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    include_filler: bool = True,
    include_silence: bool = True,
    skip_deleted: bool = True,
    with_silence: bool = True,
) -> tuple[list[label_core.LabelLine], dict[str, int]]:
    """Collect the labels and the counts the dialog reports.

    ``with_silence`` is separate from ``include_silence`` on purpose: silence
    means an ffmpeg pass over the whole episode the first time, and the preview
    must stay instant even when the download is happy to wait for the cache.
    """
    version = _version_or_raise(session, version_id)
    media = session.get(Media, version.media_id)

    tokens = [
        (row.idx, row.text, row.start_ms, row.end_ms)
        for row in session.scalars(
            select(Token).where(Token.version_id == version_id).order_by(Token.idx)
        ).all()
    ]
    if not tokens:
        raise AppError(
            "no_tokens",
            "这个版本还没有词级时间轴",
            hint="先完成转写，或换一个已经转写好的版本。",
        )

    stored = settings_store.load(session)
    fillers = frozenset(parse_word_list(str(stored["cleanup_fillers"])))

    silences: list[tuple[int, int]] = []
    if include_silence and with_silence and media is not None:
        try:
            spans = cleanup_service.load_silence_ranges(session, settings, version, media)
            # Detector spans overlap speech at their edges — ASR timestamps run
            # slightly past the sound, and silencedetect starts counting a few
            # milliseconds early. Writing the raw span puts a [SILENCE] region
            # on top of real words, so cutting it clips the tail of a sentence.
            # `find_silence_cuts` already subtracts word coverage and pads
            # against speech; reusing it keeps labels and one-click cleanup
            # agreeing about where the dead air is.
            silences = [
                (item.start_ms, item.end_ms)
                for item in find_silence_cuts(
                    tokens,
                    duration_ms=media.duration_ms,
                    silences=spans,
                    min_silence_ms=int(stored["cleanup_min_silence_ms"]),
                    pad_ms=int(stored["cleanup_silence_pad_ms"]),
                )
            ]
        except Exception as error:  # degradation is the point
            # Silence detection shells out to ffmpeg. When it fails (missing
            # binary, unreadable audio, a killed process) the *word* labels are
            # still correct and still useful; refusing the whole export because
            # a decoration could not be computed would be the wrong trade. The
            # user finds out from the counts, not from a broken download.
            log.warning("labels.silence_unavailable", version_id=version_id, error=str(error))
            silences = []

    deleted = _deleted_indices(session, version_id)
    lines = label_core.build_labels(
        tokens,
        silences=silences,
        fillers=fillers,
        include_filler=include_filler,
        include_silence=include_silence and bool(silences),
        deleted=deleted,
        skip_deleted=skip_deleted,
    )
    stats = {
        "lines": len(lines),
        "words": sum(1 for line in lines if not line.text.startswith("[")),
        "fillers": sum(1 for line in lines if line.text.startswith(label_core.TAG_FILLER)),
        "silences": sum(1 for line in lines if line.text.startswith(label_core.TAG_SILENCE)),
        "deleted": len(deleted),
        "tokens": len(tokens),
    }
    return lines, stats


def preview(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    lines: int = 20,
) -> str:
    """The first N lines exactly as the file will look."""
    built, _stats = build(
        session,
        settings,
        version_id,
        include_silence=False,
        with_silence=False,
    )
    return "".join(
        f"{label_core.format_seconds(line.start_ms)}\t{label_core.format_seconds(line.end_ms)}\t{line.text}\n"
        for line in built[:lines]
    )
