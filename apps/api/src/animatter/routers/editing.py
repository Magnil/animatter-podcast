"""Waveform and editing endpoints for the correction desk (M3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import peaks as peaks_service
from animatter.models import Media
from animatter.services import editing as edit_service
from animatter.services import transcribe as transcribe_service

router = APIRouter(prefix="/api", tags=["editing"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class BoundaryIn(BaseModel):
    action: str  # new_sentence | merge | to_prev | to_next
    from_token: int
    to_token: int


class BoundaryOut(BaseModel):
    sentences: list[dict[str, object]]


class TextEditIn(BaseModel):
    proposed: str
    # Either an explicit character selection (what the editor sends) or a
    # sentence ordinal plus the exact original text.
    token_start: int | None = None
    token_end: int | None = None
    sentence_ord: int | None = None
    original: str | None = None


class SentenceView(BaseModel):
    ord: int
    token_start: int
    token_end: int
    speaker_id: int | None
    start_ms: int
    end_ms: int
    text: str


def _media_for_version(session: Session, version: object) -> Media:
    media = session.get(Media, version.media_id)  # type: ignore[attr-defined]
    if media is None:
        raise AppError("media_not_found", "该版本对应的音频文件记录不存在", status=404)
    return media


@router.get("/projects/{project_id}/media/{media_id}/waveform")
def waveform(
    project_id: str,
    media_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    buckets_per_second: int = Query(default=64, ge=1, le=4096),
    from_ms: int = Query(default=0, ge=0),
    to_ms: int | None = Query(default=None, ge=0),
) -> dict[str, object]:
    """Peaks for one visible window.

    A 12h episode at 512 buckets/s is ~22M buckets; shipping the whole thing
    would stall the browser, so the client always asks for a window (§7.1).
    """
    settings: Settings = request.app.state.settings
    media = session.get(Media, media_id)
    if media is None:
        raise AppError("media_not_found", "音频不存在", status=404)

    project_root = settings.projects_dir / project_id
    peaks = peaks_service.load_or_build(settings.ffmpeg_path, project_root, Path(media.path), media.sha256)

    end = to_ms if to_ms is not None else peaks.duration_ms
    payload = peaks_service.window_payload(
        peaks,
        buckets_per_second=buckets_per_second,
        from_ms=from_ms,
        to_ms=min(end, peaks.duration_ms),
    )
    payload["duration_ms"] = peaks.duration_ms
    payload["levels"] = list(peaks_service.LEVELS)
    return payload


@router.get("/projects/{project_id}/media/{media_id}/audio")
def audio_stream(
    project_id: str,
    media_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> Response:
    """Serve the original audio for playback with real byte-range support.

    Three things that are easy to get subtly wrong here:

    * The MIME type must match the actual container. Sending ``audio/mpeg`` for a
      WAV makes the browser refuse to play, and the keyboard shortcuts then look
      dead even though they fire correctly.
    * ``Response`` does not implement ranges by itself. Declaring
      ``Accept-Ranges`` without handling ``Range`` means every seek re-downloads
      the whole file — untenable for a two-hour episode.
    * ``read_bytes()`` loads the whole file into memory per request.
    """
    media = session.get(Media, media_id)
    if media is None:
        raise AppError("media_not_found", "音频不存在", status=404)

    path = Path(media.path)
    if not path.exists():
        raise AppError(
            "media_file_missing",
            "音频文件已不在磁盘上",
            hint="项目文件夹可能被移动；重新登记媒体。",
            status=404,
        )

    return FileResponse(
        path,
        media_type=mime_for(media.codec, path.suffix),
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


_MIME_BY_CODEC: dict[str, str] = {
    "mp3": "audio/mpeg",
    "pcm_s16le": "audio/wav",
    "pcm_s24le": "audio/wav",
    "pcm_f32le": "audio/wav",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "vorbis": "audio/ogg",
}

_MIME_BY_SUFFIX: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


def mime_for(codec: str, suffix: str) -> str:
    """Pick a media type the browser will actually play.

    The codec column holds what ffprobe observed, so it wins over the extension:
    an episode re-encoded to mp3 but still named ``.wav`` must not be served as
    WAV.
    """
    by_codec = _MIME_BY_CODEC.get(codec.lower())
    if by_codec is not None:
        return by_codec
    return _MIME_BY_SUFFIX.get(suffix.lower(), "application/octet-stream")


@router.post("/versions/{version_id}/boundaries", response_model=BoundaryOut)
def edit_boundaries(
    version_id: str,
    payload: BoundaryIn,
    session: Session = Depends(_session_dependency),
) -> BoundaryOut:
    spans = edit_service.apply_boundary(
        session,
        version_id,
        action=payload.action,
        from_token=payload.from_token,
        to_token=payload.to_token,
    )
    tokens = transcribe_service.load_tokens(session, version_id)
    return BoundaryOut(sentences=[_view(tokens, span) for span in spans])


@router.post("/versions/{version_id}/text")
def edit_text(
    version_id: str,
    payload: TextEditIn,
    session: Session = Depends(_session_dependency),
) -> dict[str, object]:
    return edit_service.apply_text_edit(
        session,
        version_id,
        proposed=payload.proposed,
        token_start=payload.token_start,
        token_end=payload.token_end,
        sentence_ord=payload.sentence_ord,
        original=payload.original,
    )


@router.get("/versions/{version_id}/history")
def history(version_id: str, session: Session = Depends(_session_dependency)) -> list[dict[str, object]]:
    """Operation log for the history panel, newest first."""
    return [edit_service.describe_op(op) for op in edit_service.list_ops(session, version_id)]


class UndoIn(BaseModel):
    #: Omit to undo the latest operation.
    op_id: str | None = None


@router.post("/versions/{version_id}/undo")
def undo(
    version_id: str,
    session: Session = Depends(_session_dependency),
    payload: UndoIn | None = None,
) -> dict[str, object]:
    return edit_service.undo_op(session, version_id, payload.op_id if payload else None)


def _view(tokens: list, span: object) -> dict[str, object]:  # type: ignore[type-arg]
    slice_ = tokens[span.token_start : span.token_end]  # type: ignore[attr-defined]
    return {
        "ord": span.ord,  # type: ignore[attr-defined]
        "token_start": span.token_start,  # type: ignore[attr-defined]
        "token_end": span.token_end,  # type: ignore[attr-defined]
        "speaker_id": span.speaker_id,  # type: ignore[attr-defined]
        "start_ms": slice_[0].start_ms if slice_ else 0,
        "end_ms": slice_[-1].end_ms if slice_ else 0,
        "text": "".join(t.text + t.punc_after for t in slice_),
    }


class EdgeIn(BaseModel):
    token_index: int
    edge: str  # 'start' | 'end'
    new_ms: int


@router.post("/versions/{version_id}/edge")
def adjust_edge(
    version_id: str,
    payload: EdgeIn,
    session: Session = Depends(_session_dependency),
) -> dict[str, object]:
    """Move one token edge. Only the addressed token changes timing."""
    return edit_service.adjust_boundary_edge(
        session,
        version_id,
        token_index=payload.token_index,
        edge=payload.edge,
        new_ms=payload.new_ms,
    )
