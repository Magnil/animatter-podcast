"""Project archive endpoints: save a workflow to ``.project``, load it back.

Saving writes into the project directory and also offers the file for download,
because "save to local" can mean either the app's own storage or a place the user
picks. Loading always creates a *new* project rather than overwriting one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.services import archive as archive_service

router = APIRouter(prefix="/api", tags=["archive"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class ArchiveOut(BaseModel):
    path: str
    filename: str
    version: int
    saved_at: str
    project_name: str
    duration_ms: int
    sentence_count: int
    deletion_count: int
    op_count: int
    media_filename: str
    size_bytes: int


class ArchiveIn(BaseModel):
    """Where to read an archive from."""

    path: str = Field(min_length=1, description="本地 .project 文件路径")
    name: str | None = Field(default=None, description="载入后的项目名")
    media_path: str | None = Field(
        default=None,
        description="音频文件路径；音频移动过时用它重新关联",
    )


def _to_out(info: archive_service.ArchiveInfo) -> ArchiveOut:
    # Absolute, always. The archive is written relative to the data dir, so the
    # old value was a repo-relative path — meaningful only to whoever happens
    # to be standing in the repo root. The user was told "已保存", read the
    # notice, and still could not find the file; pasting that path into
    # 载入项目 then failed from anywhere else.
    return ArchiveOut(
        path=str(info.path.resolve()),
        filename=info.path.name,
        version=info.version,
        saved_at=info.saved_at,
        project_name=info.project_name,
        duration_ms=info.duration_ms,
        sentence_count=info.sentence_count,
        deletion_count=info.deletion_count,
        op_count=info.op_count,
        media_filename=info.media_filename,
        size_bytes=info.path.stat().st_size if info.path.exists() else 0,
    )


@router.post("/projects/{project_id}/archive", response_model=ArchiveOut)
def save_project(
    project_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    target: str | None = None,
) -> ArchiveOut:
    """Write the current workflow to a ``.project`` file.

    The archive carries the transcript, every boundary, every text correction,
    every deletion, the whole operation log, and the hotwords — but not the audio,
    which is referenced by hash instead.
    """
    settings: Settings = request.app.state.settings
    path = Path(target).expanduser() if target else None
    info = archive_service.save_project(session, settings, project_id, target=path)
    return _to_out(info)


@router.get("/projects/{project_id}/archive/file")
def download_archive(
    project_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    """Build the archive, hand it over as a file, and say where it landed.

    Offering only the download was not enough, and neither was offering only a
    path. The user's report was "I clicked save, nothing was downloaded, so I
    don't know where it is" — the notice named a path, but the path was
    repo-relative and no file ever reached the browser. Both halves are needed:
    a download the browser owns, and the server-side absolute path the 载入项目
    dialog can read back.

    The metadata rides along as headers rather than a second request because
    the body is the file itself. Values are percent-encoded: HTTP headers are
    latin-1, and a project named 「第 07 期」 would raise on the way out.
    """
    settings: Settings = request.app.state.settings
    info = archive_service.save_project(session, settings, project_id)
    response = FileResponse(
        info.path,
        media_type="application/zip",
        filename=info.path.name,
        content_disposition_type="attachment",
    )
    resolved = info.path.resolve()
    response.headers["X-Archive-Path"] = quote(str(resolved), safe="")
    response.headers["X-Archive-Filename"] = quote(resolved.name, safe="")
    response.headers["X-Archive-Sentences"] = str(info.sentence_count)
    response.headers["X-Archive-Ops"] = str(info.op_count)
    response.headers["X-Archive-Deletions"] = str(info.deletion_count)
    return response


@router.post("/archive/inspect", response_model=ArchiveOut)
def inspect_archive(payload: ArchiveIn) -> ArchiveOut:
    """Read an archive's metadata without importing it.

    The UI shows this before loading so the user can confirm it is the episode
    they meant, rather than discovering it after a second project appears.
    """
    info = archive_service.inspect_archive(Path(payload.path).expanduser())
    return _to_out(info)


@router.post("/archive/load")
def load_archive(
    payload: ArchiveIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> dict[str, object]:
    """Import an archive as a new project and return its id.

    ``media_relinked`` is false when the audio file could not be found or its hash
    differs. That is a warning, not a failure: the transcript and every edit are
    still restored, and losing them because a file moved would be worse.
    """
    settings: Settings = request.app.state.settings
    try:
        project, version, relinked = archive_service.load_project(
            session,
            settings,
            Path(payload.path).expanduser(),
            name=payload.name,
            media_path=payload.media_path,
        )
    except AppError:
        raise

    log.info("archive.loaded", project=project.id, relinked=relinked)
    return {
        "project_id": project.id,
        "project_name": project.name,
        "version_id": version.id,
        "sentence_count": version.sentence_count,
        "duration_ms": version.duration_ms,
        "media_relinked": relinked,
        "warning": (
            "" if relinked else "音频文件未找到或已变更，文稿与编辑已恢复；重新关联音频后才能试听与导出。"
        ),
    }
