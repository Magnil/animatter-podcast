"""Clipping endpoints: deletions, EDL preview, and rendering."""

from __future__ import annotations

import shutil
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
from animatter.models import Render
from animatter.services import clipping as clipping_service
from animatter.services import renders as renders_service

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


class RestoreWordsIn(BaseModel):
    """Which words to bring back from the deletion set."""

    token_ids: list[int]


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
    progress: float
    error: str | None = None


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
    """Delete by token ids or by an explicit range.

    Deliberately **no** video-anchor refresh here: a deletion removes audio, but it
    does not move a single token, so the sentence-start grid this timeline snaps to is
    untouched. What a deletion *does* change is how long each block plays, and that is
    derived on read (``services/video.view_items``) rather than stored — so there is
    nothing to repair on this path, and recompiling the EDL per deletion would be pure
    cost on the keystroke that has to feel instant.
    """
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


@router.post("/versions/{version_id}/deletions/restore", response_model=list[DeletionOut])
def restore_token_deletion(
    version_id: str, payload: RestoreWordsIn, session: Session = Depends(_session_dependency)
) -> list[DeletionOut]:
    """Take back the deletion of these words, leaving the rest of each cut cut.

    Word-scoped rather than row-scoped: the editor's 「还」 button is pressed on a
    word, and a sentence delete covers dozens of them. Restoring the row would
    bring back audio the user never pointed at.
    """
    rows = clipping_service.restore_token_deletion(session, version_id, payload.token_ids)
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


class PickFolderOut(BaseModel):
    path: str


@router.post("/system/pick-folder", response_model=PickFolderOut)
def pick_folder(initial: str = Query(default="")) -> PickFolderOut:
    """Open the OS's own folder chooser and return what the user picked.

    A browser cannot read arbitrary local folders, but this application's server *is* on the
    user's machine, so the chooser can be the real one. `tkinter` is used rather than a Win32
    call because it ships with CPython on Windows and needs no extra dependency.

    Failure is a normal outcome, not a crash: no display (a test run, a headless box) or a
    dismissed dialog both mean "no folder chosen", and the dialog's text field stays usable.
    So this returns 409 with a hint that says exactly that.
    """
    import tkinter
    from tkinter import filedialog

    try:
        root = tkinter.Tk()
        root.withdraw()
        # `topmost` because the app is a browser window in front of everything: without it the
        # chooser opens *behind* the browser and looks like a button that did nothing.
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title="选择导出文件夹",
            initialdir=initial.strip() or str(default_export_dir()),
        )
        root.destroy()
    except Exception as exc:  # any Tk failure means there is no chooser here
        log.warning("pick_folder.unavailable", error=str(exc))
        raise AppError(
            "folder_picker_unavailable",
            "这台机器上没有可用的文件夹对话框",
            hint="直接在输入框里填完整路径即可，例如 D:/导出。",
            status=409,
        ) from exc
    if not chosen:
        raise AppError(
            "folder_picker_cancelled",
            "没有选择文件夹",
            hint="取消不会改变原来的路径。",
            status=409,
        )
    return PickFolderOut(path=str(Path(chosen)))


class SaveToIn(BaseModel):
    """Where the finished file should be put. Both fields are the user's choice."""

    #: 目标文件夹的绝对路径。空字符串表示「应用默认导出目录」（系统的「下载」）。
    out_dir: str = ""
    #: 文件名，可以带也可以不带后缀；后缀按成品实际格式补，**不允许**改成别的格式。
    filename: str = ""


class SavedFileOut(BaseModel):
    path: str
    bytes: int
    filename: str


#: Suffixes a user might type that say "a media file", not "this container". The container is
#: decided by the export path (mp4 for a film, m4a/wav for audio), so any of these is replaced
#: rather than kept.
_MEDIA_SUFFIXES = (".mp4", ".m4a", ".wav", ".mov", ".mkv", ".mp3", ".aac", ".flac", ".webm")


def resolve_export_target(
    settings: Settings,
    *,
    out_dir: str,
    filename: str,
    source: Path,
) -> Path:
    """Work out where a delivered file goes, creating the folder.

    Three rules, each of which exists because the naive version was wrong:

    1. **The suffix comes from the finished file, not from what the user typed.** Typing
       ``成片.wav`` for an M4A export would otherwise write an m4a with a .wav name, which
       some players refuse and every user reads as a bug.
    2. **A relative or empty folder means the default one**, so an untouched form still
       delivers somewhere findable instead of raising.
    3. **Characters Windows forbids are replaced**, not rejected: the film's name comes from
       the episode title, and 「第 7 期：罗小黑 / 专访」 is a perfectly good title.
    """
    folder = Path(out_dir).expanduser() if out_dir.strip() else default_export_dir()
    if not folder.is_absolute():
        folder = default_export_dir() / folder
    folder.mkdir(parents=True, exist_ok=True)

    stem = filename.strip()
    # Strip **any** known media suffix, not only the one this file happens to have: typing
    # 「成片.wav」 for an M4A export must give 「成片.m4a」, not 「成片.wav.m4a」. Only known
    # media suffixes are stripped — 「第 7 期 · v1.2」 keeps its 「.2」.
    lowered = stem.lower()
    for suffix in _MEDIA_SUFFIXES:
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    for forbidden in '\\/:*?"<>|':
        stem = stem.replace(forbidden, "_")
    stem = stem.strip(" .") or "成片"
    return folder / f"{stem}{source.suffix.lower()}"


def default_export_dir() -> Path:
    """Where an export goes when the user does not say. Windows' 下载, else the home dir."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


@router.post("/versions/{version_id}/render", response_model=RenderOut)
def render_version(
    version_id: str,
    request: Request,
    preview: bool = Query(default=False),
    lossless: bool = Query(default=False),
    from_ms: int | None = Query(default=None),
    to_ms: int | None = Query(default=None),
) -> RenderOut:
    """Queue a render and return immediately.

    This used to encode inside the request. On a 105-minute episode that is about
    twelve minutes of blocking, during which the button looks dead and every other
    write in the app fails with ``database is locked`` — one open write
    transaction, one SQLite writer. The encode now runs on a worker thread and the
    client polls ``GET /api/renders/{id}`` for progress.
    """
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    window = (from_ms, to_ms) if from_ms is not None and to_ms is not None else None
    started = renders_service.start(
        db, settings, version_id, preview=preview, lossless=lossless, window=window
    )
    return _to_out(started.render)


@router.get("/renders/{render_id}", response_model=RenderOut)
def render_status(
    render_id: str, session: Session = Depends(_session_dependency)
) -> RenderOut:
    return _to_out(renders_service.get(session, render_id))


def _to_out(record: Render) -> RenderOut:
    return RenderOut(
        id=record.id,
        duration_ms=record.duration_ms,
        status=record.status,
        url=f"/api/renders/{record.id}/file",
        progress=record.progress,
        error=record.error,
    )


@router.post("/renders/{render_id}/save-to", response_model=SavedFileOut)
def save_render_to(
    render_id: str,
    payload: SaveToIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> SavedFileOut:
    """Copy a finished render to the folder and file name the user chose (round 135).

    The render itself lives in the app's data directory under a name derived from the EDL
    hash, which is right for caching and useless for delivery. This is the delivery step:
    one copy, to a path the user picked, with a name they typed.

    A copy rather than a move: the cached render is what makes a second export of an
    unchanged edit free, and moving it away would turn that into a full re-encode.
    """
    record = session.get(Render, render_id)
    if record is None:
        raise AppError("render_not_found", "导出任务不存在", status=404)
    if record.status != "done":
        raise AppError(
            "render_not_done",
            "这个导出还没完成",
            hint="等进度走完再保存；停止的导出没有成品。",
            status=409,
        )
    source = Path(record.out_path)
    if not source.is_file():
        raise AppError(
            "render_file_missing",
            "导出文件已被清理",
            hint="重新导出即可。",
            status=404,
        )

    settings: Settings = request.app.state.settings
    target = resolve_export_target(
        settings,
        out_dir=payload.out_dir,
        filename=payload.filename,
        source=source,
    )
    if target != source:
        shutil.copyfile(source, target)
    size = target.stat().st_size
    log.info("render.saved", render_id=render_id, path=str(target), bytes=size)
    return SavedFileOut(path=str(target), bytes=size, filename=target.name)


@router.get("/renders/{render_id}/file")
def render_file(
    render_id: str, request: Request, session: Session = Depends(_session_dependency)
) -> FileResponse:
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
