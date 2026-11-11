"""Video template lifecycle: import, compile, confirm, and slot geometry (§19.2).

The compile is a background job, not a request-time operation. Measured cost on the
sample 2560×1440 / 58-layer PSD is ~6 s (open 0.44 s, background composite 4.7 s,
overlay 0.8 s, resize 0.16 s). Six seconds inside a POST would look like a dead
button, and the same job-row / poll / progress machinery already exists for audio
renders, so it is reused rather than reinvented.

Two things make repeated imports cheap:

* the compiled artefacts depend only on the PSD's bytes, so ``source_sha256``
  short-circuits a re-import of the same file;
* the artefacts are keyed by that hash, so compiling a *different* file cannot
  overwrite the previous one's PNGs while a render is still reading them.

A failed compile leaves the row in ``failed`` with a human sentence in ``error``. The
row is kept rather than deleted: the confirm screen needs somewhere to show why the
file was rejected, and a vanished row reads as "the import did nothing".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import template as template_media
from animatter.models import Project, VideoTemplate
from animatter.services import projects as project_service

log = get_logger(__name__)

#: Compiled artefacts live under the project, not in a shared cache: deleting a
#: project must take its template with it, and two projects may legitimately import
#: different files.
_VIDEO_SUBDIR = "video"

FITS = ("contain", "cover", "blur_fill", "stretch")


@dataclass(frozen=True)
class TemplateView:
    row: VideoTemplate
    slots: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        """The template as the browser needs it.

        ``slots.texts`` is the **resolved** list — PSD defaults with the user's edits applied
        — because that is what the film will show, and showing the defaults while the render
        uses the overrides is how a UI ends up lying about its own output.

        Avatar art URLs point at the *effective* files, so a replaced photo is visible in
        the panel without the browser having to know that overrides exist.
        """
        from animatter.services import video_edits

        slots = dict(self.slots)
        slots["texts"] = video_edits.resolved_texts(self.row)
        avatars = slots.get("avatars")
        if isinstance(avatars, list):
            replaced = video_edits.edits_of(self.row)["photos"]
            slots["avatars"] = [
                _avatar_view(self.row, item, replaced) for item in avatars if isinstance(item, dict)
            ]
        return {
            "id": self.row.id,
            "status": self.row.status,
            "error": self.row.error,
            "canvas_w": self.row.canvas_w,
            "canvas_h": self.row.canvas_h,
            "box_rect": json.loads(self.row.box_rect or "{}"),
            "box_fit_default": self.row.box_fit_default,
            "background_url": asset_url(self.row.project_id, self.row.background_path, self.row.status),
            "overlay_url": asset_url(self.row.project_id, self.row.overlay_path, self.row.status),
            "slots": slots,
            "compiled_at": self.row.compiled_at,
        }


def _avatar_view(
    row: VideoTemplate, slot: dict[str, object], replaced: dict[str, object]
) -> dict[str, object]:
    """One avatar slot: the compiled fields, plus URLs for its *effective* art.

    The compiled ``lit_path``/``dim_path`` are left as they are rather than replaced by URLs.
    Two consumers read this list — the settings panel wants URLs, and the tests (plus the
    export path) assert that the compiled files exist at the paths the compile wrote. Renaming
    would have forced both to change, and the paths are the honest description of what is on
    disk; the URLs are the browser's view of the same thing.
    """
    from animatter.services import video_edits

    name = str(slot.get("name") or "")
    lit = str(slot.get("lit_path") or "")
    dim = str(slot.get("dim_path") or "")
    replacement = video_edits.photo_paths(row, slot)
    if replacement is not None:
        lit, dim = str(replacement[0]), str(replacement[1])
    merged = dict(slot)
    merged["name"] = name
    # The *effective* paths, so a replaced photo is also correct for a caller that reads the
    # slot list instead of the avatars endpoint.
    merged["lit_path"] = str(lit)
    merged["dim_path"] = str(dim)
    merged["lit_url"] = asset_url(row.project_id, lit, row.status)
    merged["dim_url"] = asset_url(row.project_id, dim, row.status)
    merged["photo_replaced"] = name in replaced
    return merged


def asset_url(project_id: str, path: str, status: str) -> str:
    """Turn a stored path into an API URL, or ``""`` when there is nothing to serve.

    The project id is part of the URL because the route is project-scoped, and only the
    *file name* appears in it — an absolute filesystem path never reaches the browser.
    Forgetting the id here produced a URL that 404'd while looking perfectly valid,
    which is exactly why the router test fetches the URL the response hands back
    instead of building one itself.
    """
    if status != "ready" or not path:
        return ""
    return f"/api/projects/{project_id}/video/template/asset/{Path(path).name}"


def video_dir(settings: Settings, project_id: str) -> Path:
    return project_service.project_root(settings, project_id) / _VIDEO_SUBDIR


def templates_dir(settings: Settings, project_id: str) -> Path:
    return video_dir(settings, project_id) / "templates"


def get(session: Session, project_id: str) -> VideoTemplate | None:
    return session.scalars(
        select(VideoTemplate)
        .where(VideoTemplate.project_id == project_id)
        .order_by(VideoTemplate.created_at.desc())
    ).first()


def require(session: Session, project_id: str) -> VideoTemplate:
    row = get(session, project_id)
    if row is None:
        raise AppError(
            "video_template_missing",
            "这个项目还没有导入视频模板",
            hint="在「视频」里导入一份 16:9 的 PSD 模板。",
            status=404,
        )
    return row


def view(session: Session, project_id: str) -> TemplateView | None:
    row = get(session, project_id)
    if row is None:
        return None
    return TemplateView(row=row, slots=json.loads(row.slots_json or "{}"))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_template(
    session: Session,
    settings: Settings,
    project_id: str,
    source: Path,
) -> VideoTemplate:
    """Register a PSD for this project and mark it pending a compile.

    Re-importing the same bytes reuses the existing ``ready`` row instead of
    replacing it: the compiled PNGs would be identical, and replacing the row would
    break every URL the browser is already holding.
    """
    if session.get(Project, project_id) is None:
        raise AppError("project_not_found", f"项目 {project_id} 不存在", status=404)

    resolved = source.expanduser()
    if not resolved.is_file():
        raise AppError(
            "template_file_missing",
            f"找不到 {source.name}",
            hint="确认文件路径，或把它放到项目目录下再导入。",
            status=404,
        )
    if resolved.suffix.lower() != ".psd":
        raise AppError(
            "template_not_psd",
            f"{resolved.name} 不是 PSD",
            hint="模板只接受 Photoshop 的 .psd 文件。",
        )

    digest = sha256_of(resolved)
    existing = get(session, project_id)
    if existing is not None and existing.source_sha256 == digest and existing.status == "ready":
        log.info("template.reused", project=project_id, template=existing.id)
        return existing

    # Keep the source next to the compile output so the project stays portable: a
    # moved or deleted original would make 「重新编译」 impossible later.
    target_dir = templates_dir(settings, project_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = target_dir / f"source-{digest[:12]}.psd"
    if not stored.exists():
        stored.write_bytes(resolved.read_bytes())

    if existing is not None:
        existing.source_path = str(stored)
        existing.source_sha256 = digest
        existing.status = "pending"
        existing.error = ""
        existing.compiled_at = None
        row = existing
    else:
        row = VideoTemplate(
            id=new_id("vtpl"),
            project_id=project_id,
            source_path=str(stored),
            source_sha256=digest,
            canvas_w=0,
            canvas_h=0,
            background_path="",
            overlay_path="",
            box_rect="{}",
            box_fit_default="blur_fill",
            slots_json="{}",
            status="pending",
            error="",
            created_at=utcnow_iso(),
        )
        session.add(row)
    session.flush()
    log.info("template.imported", project=project_id, template=row.id, sha256=digest[:12])
    return row


def compile_now(session: Session, template_id: str) -> VideoTemplate:
    """Compile synchronously. The worker calls this; tests call it directly.

    Errors are **recorded**, not raised: the row has to survive so the UI can show
    why the file was rejected, and a raising background worker would only log.
    """
    row = session.get(VideoTemplate, template_id)
    if row is None:
        raise AppError("video_template_missing", "模板记录不存在", status=404)

    out_dir = Path(row.source_path).parent / f"build-{row.source_sha256[:12]}"
    row.status = "compiling"
    session.flush()

    try:
        compiled = template_media.compile_template(Path(row.source_path), out_dir)
    except AppError as exc:
        row.status = "failed"
        row.error = f"{exc.message}。{exc.hint}".strip()
        session.flush()
        log.warning("template.compile_failed", template=template_id, code=exc.code)
        return row
    except Exception as exc:  # a crash must not leave the row "compiling" forever
        row.status = "failed"
        row.error = f"编译模板时出错：{type(exc).__name__}: {exc}"
        session.flush()
        log.exception("template.compile_crashed", template=template_id)
        return row

    row.canvas_w = compiled.canvas_w
    row.canvas_h = compiled.canvas_h
    row.background_path = str(compiled.background_path)
    row.overlay_path = str(compiled.overlay_path)
    row.box_rect = json.dumps(compiled.box_rect.as_dict())
    row.slots_json = json.dumps(compiled.slots_json(), ensure_ascii=False)
    row.status = "ready"
    row.error = ""
    row.compiled_at = utcnow_iso()
    session.flush()
    log.info(
        "template.compiled",
        template=template_id,
        box=compiled.box_rect.as_dict(),
        avatars=len(compiled.avatars),
    )
    return row


def update_options(session: Session, project_id: str, *, box_fit_default: str | None) -> VideoTemplate:
    row = require(session, project_id)
    if box_fit_default is not None:
        if box_fit_default not in FITS:
            raise AppError(
                "video_fit_invalid",
                f"未知的填充方式 {box_fit_default}",
                hint=f"可选：{'、'.join(FITS)}。",
            )
        row.box_fit_default = box_fit_default
    session.flush()
    return row


def asset_path(session: Session, project_id: str, name: str) -> Path:
    """Resolve one compiled artefact by file name, refusing anything outside it.

    ``Path(name).name`` is the whole guard: a request for ``../../app.db`` must not be
    able to read the database through the "serve the template PNG" route. This is a
    local single-user app, but a route that serves arbitrary files is a bug regardless
    of who is calling it.

    Two directories are searched: the compile output, and its ``edits/`` subdirectory,
    where a replaced avatar photo is written. Both are inside the template's own build
    directory, so the same ``.name`` guard covers both — and a replacement cannot be served
    by a path that reaches outside them.
    """
    row = get(session, project_id)
    if row is None or not row.background_path:
        raise AppError("template_not_compiled", "模板还没编译好", status=409)

    base = Path(row.background_path).parent
    safe = Path(name).name
    for folder in (base, base / "edits"):
        candidate = folder / safe
        if candidate.is_file():
            return candidate
    raise AppError("template_asset_missing", f"模板里没有 {name}", status=404)


def box_rect_of(row: VideoTemplate) -> dict[str, int]:
    data: dict[str, int] = json.loads(row.box_rect or "{}")
    return data
