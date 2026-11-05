"""Clipping endpoints: deletions, EDL preview, and rendering."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.services import clipping as clipping_service

router = APIRouter(prefix="/api", tags=["clipping"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class DeletionIn(BaseModel):
    """Either a token selection or an explicit waveform range."""

    token_ids: list[int] | None = None
    start_ms: int | None = None
    end_ms: int | None = None


class DeletionOut(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    source: str


class EdlOut(BaseModel):
    original_duration_ms: int
    output_duration_ms: int
    removed_ms: int
    segment_count: int
    keeps: list[dict[str, int]]
    deletions: list[dict[str, object]]
    clamps: list[dict[str, object]]
    signature: str


class RenderOut(BaseModel):
    id: str
    duration_ms: int
    status: str
    url: str


@router.get("/versions/{version_id}/deletions", response_model=list[DeletionOut])
def list_deletions(version_id: str, session: Session = Depends(_session_dependency)) -> list[DeletionOut]:
    rows = clipping_service.list_deletions(session, version_id)
    return [
        DeletionOut(id=row.id, start_ms=row.start_ms, end_ms=row.end_ms, source=row.source) for row in rows
    ]


@router.post("/versions/{version_id}/deletions", response_model=list[DeletionOut], status_code=201)
def add_deletion(
    version_id: str, payload: DeletionIn, session: Session = Depends(_session_dependency)
) -> list[DeletionOut]:
    if payload.token_ids:
        rows = clipping_service.add_token_deletion(session, version_id, payload.token_ids)
    elif payload.start_ms is not None and payload.end_ms is not None:
        rows = [
            clipping_service.add_deletion(
                session,
                version_id,
                start_ms=payload.start_ms,
                end_ms=payload.end_ms,
                source="range",
            )
        ]
    else:
        raise AppError(
            "deletion_target_missing",
            "没有指定要删除的内容",
            hint="传 token_ids（字级删除）或 start_ms/end_ms（波形框选）。",
        )
    return [
        DeletionOut(id=row.id, start_ms=row.start_ms, end_ms=row.end_ms, source=row.source) for row in rows
    ]


@router.delete("/deletions/{deletion_id}", status_code=204)
def delete_deletion(deletion_id: str, session: Session = Depends(_session_dependency)) -> None:
    clipping_service.remove_deletion(session, deletion_id)


@router.get("/versions/{version_id}/edl", response_model=EdlOut)
def get_edl(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    snap: bool = Query(default=True, description="是否吸附到静音谷"),
) -> EdlOut:
    settings: Settings = request.app.state.settings
    from animatter.services import clipping as svc

    version = svc.get_version(session, version_id)
    edl = svc.build_edl(session, settings, version, snap=snap)
    view = svc.view(edl)
    return EdlOut(**view.__dict__)


@router.post("/versions/{version_id}/render", response_model=RenderOut)
def render_version(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    preview: bool = Query(default=False),
    lossless: bool = Query(default=False),
    from_ms: int | None = Query(default=None),
    to_ms: int | None = Query(default=None),
) -> RenderOut:
    """Render the current EDL.

    Synchronous on purpose for now: a two-hour render takes minutes, and the UI
    milestone that needs a job queue is M5's export. Preview renders are short by
    construction.
    """
    settings: Settings = request.app.state.settings
    window = (from_ms, to_ms) if from_ms is not None and to_ms is not None else None
    record = clipping_service.render_version(
        session, settings, version_id, preview=preview, window=window, lossless=lossless
    )
    return RenderOut(
        id=record.id,
        duration_ms=record.duration_ms,
        status=record.status,
        url=f"/api/renders/{record.id}/file",
    )


@router.get("/renders/{render_id}/file")
def render_file(
    render_id: str, request: Request, session: Session = Depends(_session_dependency)
) -> FileResponse:
    from animatter.models import Render

    record = session.get(Render, render_id)
    if record is None:
        raise AppError("render_not_found", "渲染结果不存在", status=404)
    path = Path(record.out_path)
    if not path.exists():
        raise AppError(
            "render_file_missing",
            "渲染文件已被清理",
            hint="重新渲染即可。",
            status=404,
        )
    suffix = path.suffix.lower()
    media_type = "audio/wav" if suffix == ".wav" else "audio/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
    )
