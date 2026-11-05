"""Transcription and transcript-reading endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Media, TranscriptVersion
from animatter.services import hotwords as hotword_service
from animatter.services import transcribe as transcribe_service

router = APIRouter(prefix="/api", tags=["transcribe"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class MediaRegisterIn(BaseModel):
    path: str = Field(description="本地音频绝对路径")


class MediaOut(BaseModel):
    id: str
    project_id: str
    duration_ms: int
    channels: int
    sample_rate: int
    codec: str


class TranscribeIn(BaseModel):
    media_id: str
    model: str | None = None
    language_hints: list[str] = ["zh", "en"]
    channel_id: int = 0
    diarization: bool = True
    speaker_count: int | None = 2
    #: ``True`` → merge the project's global and episode hotwords into
    #: ``parameters.vocabulary``. The client used to send this flag only for the
    #: field to be silently dropped by pydantic, so *every* transcription ran
    #: with an empty vocabulary no matter what the UI said (AGENT.md §2.1).
    use_hotwords: bool = False
    #: Explicit override; when omitted and ``use_hotwords`` is set, the stored
    #: glossary is used. Kept for the CLI and for replaying a historical run.
    vocabulary: dict[str, int] | None = None
    vocabulary_id: str | None = None
    context_text: str | None = None


class VersionOut(BaseModel):
    id: str
    version: int
    duration_ms: int
    token_count: int
    sentence_count: int
    created_at: str
    #: How many glossary terms actually went into ``parameters.vocabulary``.
    #: Zero is a real, reportable answer — it is what the UI needs to tell
    #: "hotwords were off" apart from "hotwords were on but the list was empty".
    hotword_count: int = 0


class TokenOut(BaseModel):
    idx: int
    start_ms: int
    end_ms: int
    text: str
    kind: str
    punc_after: str
    derived: bool
    #: A human rewrote this word; the editor paints it blue.
    revised: bool = False


class SentenceOut(BaseModel):
    id: str
    ord: int
    token_start: int
    token_end: int
    speaker_id: int | None
    text: str
    start_ms: int
    end_ms: int


class TranscriptOut(BaseModel):
    version: VersionOut
    sentences: list[SentenceOut]
    tokens: list[TokenOut]


@router.post("/projects/{project_id}/media", response_model=MediaOut, status_code=201)
def register_media(
    project_id: str,
    payload: MediaRegisterIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> MediaOut:
    settings = request.app.state.settings
    source = Path(payload.path).expanduser()
    media = transcribe_service.register_media(session, settings, project_id, source)
    return MediaOut(
        id=media.id,
        project_id=media.project_id,
        duration_ms=media.duration_ms,
        channels=media.channels,
        sample_rate=media.sample_rate,
        codec=media.codec,
    )


@router.get("/projects/{project_id}/media", response_model=list[MediaOut])
def list_media(project_id: str, session: Session = Depends(_session_dependency)) -> list[MediaOut]:
    rows = session.scalars(select(Media).where(Media.project_id == project_id)).all()
    return [
        MediaOut(
            id=row.id,
            project_id=row.project_id,
            duration_ms=row.duration_ms,
            channels=row.channels,
            sample_rate=row.sample_rate,
            codec=row.codec,
        )
        for row in rows
    ]


@router.post(
    "/projects/{project_id}/transcribe",
    response_model=VersionOut,
    status_code=201,
)
def transcribe(
    project_id: str,
    payload: TranscribeIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> VersionOut:
    """Synchronous transcription.

    Long episodes legitimately run for minutes, which is why this is exposed for
    the CLI milestone and *not* the shape the UI will use: M1's goal is to prove
    the chain, and a job queue arrives with the editor.
    """
    settings = request.app.state.settings
    media = session.get(Media, payload.media_id)
    if media is None:
        raise AppError(
            "media_not_found",
            f"媒体 {payload.media_id} 不存在",
            hint="先调用 POST /api/projects/{id}/media 登记音频文件。",
            status=404,
        )

    vocabulary = payload.vocabulary
    trimmed = 0
    if vocabulary is None and payload.use_hotwords:
        # Merge the glossary here rather than in the service: the transcript job
        # snapshot has to record what was *actually* sent, and this is the only
        # place that knows the project + media pair the glossary is keyed by.
        vocabulary, trimmed = hotword_service.effective_vocabulary(
            session, project_id=project_id, media_id=payload.media_id
        )
        log.info(
            "transcribe.hotwords",
            project_id=project_id,
            media_id=payload.media_id,
            terms=len(vocabulary),
            trimmed=trimmed,
        )

    options = transcribe_service.TranscribeOptions(
        model=payload.model or settings.asr_model,
        language_hints=tuple(payload.language_hints),
        channel_id=payload.channel_id,
        diarization=payload.diarization,
        speaker_count=payload.speaker_count,
        vocabulary=vocabulary,
        vocabulary_id=payload.vocabulary_id,
        context_text=payload.context_text,
    )
    version = transcribe_service.transcribe_media(
        session, settings, project_id=project_id, media=media, options=options
    )
    return VersionOut(
        id=version.id,
        version=version.version,
        duration_ms=version.duration_ms,
        token_count=version.token_count,
        sentence_count=version.sentence_count,
        created_at=version.created_at,
        hotword_count=len(vocabulary or {}),
    )


@router.get("/projects/{project_id}/versions", response_model=list[VersionOut])
def list_versions(project_id: str, session: Session = Depends(_session_dependency)) -> list[VersionOut]:
    rows = session.scalars(
        select(TranscriptVersion)
        .where(TranscriptVersion.project_id == project_id)
        .order_by(TranscriptVersion.version.desc())
    ).all()
    return [
        VersionOut(
            id=row.id,
            version=row.version,
            duration_ms=row.duration_ms,
            token_count=row.token_count,
            sentence_count=row.sentence_count,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/versions/{version_id}/transcript", response_model=TranscriptOut)
def get_transcript(
    version_id: str,
    session: Session = Depends(_session_dependency),
    from_sentence: int = 0,
    limit: int = 200,
) -> TranscriptOut:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)

    tokens = transcribe_service.load_tokens(session, version_id)
    sentences = transcribe_service.load_sentences(session, version_id)
    window = sentences[from_sentence : from_sentence + max(1, min(limit, 500))]

    sentence_rows = []
    for span in window:
        slice_ = tokens[span.token_start : span.token_end]
        sentence_rows.append(
            SentenceOut(
                id=f"{version_id}:{span.ord}",
                ord=span.ord,
                token_start=span.token_start,
                token_end=span.token_end,
                speaker_id=span.speaker_id,
                text="".join(t.text + t.punc_after for t in slice_),
                start_ms=slice_[0].start_ms if slice_ else 0,
                end_ms=slice_[-1].end_ms if slice_ else 0,
            )
        )

    # Tokens are returned for the requested window only; a 3h episode is ~40k
    # tokens and shipping them all would make the first paint unusable.
    first = window[0].token_start if window else 0
    last = window[-1].token_end if window else 0
    token_rows = [
        TokenOut(
            idx=t.idx,
            start_ms=t.start_ms,
            end_ms=t.end_ms,
            text=t.text,
            kind=t.kind,
            punc_after=t.punc_after,
            derived=t.derived,
            revised=t.revised,
        )
        for t in tokens[first:last]
    ]

    return TranscriptOut(
        version=VersionOut(
            id=version.id,
            version=version.version,
            duration_ms=version.duration_ms,
            token_count=version.token_count,
            sentence_count=version.sentence_count,
            created_at=version.created_at,
        ),
        sentences=sentence_rows,
        tokens=token_rows,
    )


@router.get("/versions/{version_id}/stats")
def version_stats(version_id: str, session: Session = Depends(_session_dependency)) -> dict[str, Any]:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", status=404)
    tokens = transcribe_service.load_tokens(session, version_id)
    derived = sum(1 for token in tokens if token.derived)
    return {
        "token_count": len(tokens),
        "derived_tokens": derived,
        "derived_ratio": round(derived / len(tokens), 4) if tokens else 0.0,
        "sentence_count": version.sentence_count,
        "duration_ms": version.duration_ms,
        "speech_end_ms": max((t.end_ms for t in tokens), default=0),
    }
