"""Audacity label export endpoint: one request, file comes back.

Deliberately *not* the two-step "POST to create an export row, then GET it by
id" shape the subtitle export uses. That shape exists because subtitle exports
are kept and listed; a label file is a transient hand-off in the middle of an
edit, and a download that first writes state is a download that can fail after
writing it. This endpoint reads the transcript and streams the bytes.

Counts ride along in ``X-Labels-*`` headers so the dialog can report what it
wrote without a second request. They are percent-encoded, not because the values
are non-ASCII — the filename is, and one rule for both is easier to keep.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core import labels as label_core
from animatter.db import Database
from animatter.services import labels as label_service

router = APIRouter(prefix="/api", tags=["labels"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class LabelsInfo(BaseModel):
    version_id: str
    filename: str
    lines: int
    words: int
    fillers: int
    silences: int
    deleted: int
    tokens: int


@router.get("/versions/{version_id}/labels/preview")
def preview_labels(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    limit: int = Query(default=20, ge=1, le=200),
) -> PlainTextResponse:
    """The first lines exactly as the file will look, for the dialog."""
    settings: Settings = request.app.state.settings
    text = label_service.preview(session, settings, version_id, lines=limit)
    return PlainTextResponse(text)


@router.get("/versions/{version_id}/labels/info", response_model=LabelsInfo)
def labels_info(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    include_filler: bool = Query(default=True),
    include_silence: bool = Query(default=True),
    skip_deleted: bool = Query(default=True),
) -> LabelsInfo:
    """Counts only — no silence detection, so this stays instant."""
    settings: Settings = request.app.state.settings
    _lines, stats = label_service.build(
        session,
        settings,
        version_id,
        include_filler=include_filler,
        include_silence=include_silence,
        skip_deleted=skip_deleted,
        with_silence=False,
    )
    return LabelsInfo(
        version_id=version_id,
        filename=label_service.filename_for(session, version_id),
        **stats,
    )


@router.get("/versions/{version_id}/labels/file")
def download_labels(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    include_filler: bool = Query(default=True),
    include_silence: bool = Query(default=True),
    skip_deleted: bool = Query(default=True),
) -> Response:
    settings: Settings = request.app.state.settings
    lines, stats = label_service.build(
        session,
        settings,
        version_id,
        include_filler=include_filler,
        include_silence=include_silence,
        skip_deleted=skip_deleted,
    )
    filename = label_service.filename_for(session, version_id)
    body = label_core.render_audacity(lines)
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={
            # Headers are latin-1; 「漫物质」 is not. Percent-encode on the way
            # out, decode on the way in — the archive download does the same.
            "X-Labels-Filename": quote(filename),
            "X-Labels-Lines": str(stats["lines"]),
            "X-Labels-Words": str(stats["words"]),
            "X-Labels-Fillers": str(stats["fillers"]),
            "X-Labels-Silences": str(stats["silences"]),
            "X-Labels-Deleted": str(stats["deleted"]),
            "X-Labels-Tokens": str(stats["tokens"]),
        },
    )
