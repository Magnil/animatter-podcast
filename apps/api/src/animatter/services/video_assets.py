"""The asset library: images and video clips the user can drop into the media box (§19.4).

Two decisions worth stating, because both are the opposite of the obvious one.

**Files are copied, not referenced.** The user's material lives wherever it lives — a
Downloads folder, a shared drive, a phone sync directory. A timeline that points at
those paths works exactly once, and then breaks silently when the folder is reorganised
and the export fails fourteen minutes in with 「文件不见了」. Copying costs a few megabytes
and makes the project self-contained, which is the same trade the PSD template already
makes.

**Deduplication is by SHA-256, scoped to the project.** Importing the same screenshot
twice is the normal way a gallery gets built (drag it in, decide later, drag it again),
and without the unique constraint the library fills with visually identical rows that
are impossible to tell apart in a picker. The hash is per project rather than global so
one project's delete cannot orphan another's timeline.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import Project, VideoAsset
from animatter.services import projects as project_service

log = get_logger(__name__)

#: Extensions we will take. Deliberately a list rather than "whatever ffmpeg can open":
#: a mis-suffixed file that ffprobe *can* read is fine, but accepting `.exe` because the
#: demuxer happened to guess is not a feature.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"})

#: A 200 MB video in a podcast slide is not a use case this feature serves, and letting
#: one in means every export re-reads and re-encodes it. Generous enough for a phone clip.
MAX_BYTES = 512 * 1024 * 1024

KIND_IMAGE = "image"
KIND_VIDEO = "video"


@dataclass(frozen=True)
class AssetView:
    row: VideoAsset
    url: str
    exists: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.row.id,
            "kind": self.row.kind,
            "name": Path(self.row.path).name,
            "sha256": self.row.sha256,
            "width": self.row.width,
            "height": self.row.height,
            "duration_ms": self.row.duration_ms,
            "created_at": self.row.created_at,
            "url": self.url,
            "exists": self.exists,
        }


def asset_dir(settings: Settings, project_id: str) -> Path:
    path = project_service.project_root(settings, project_id) / "video" / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: The rule that decides whether a file is a usable image or video, and reads its size.
#:
#: Done with ffprobe for **both** kinds rather than with Pillow for images: an iPhone
#: JPEG with a rotation tag has its pixels stored sideways, and Pillow reports the
#: *stored* size while every viewer (and ffmpeg) shows it rotated. Using one tool for
#: both means the aspect the library reports is the aspect the renderer produces.
_PROBE_ARGS = [
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_streams",
    "-show_format",
]


def _probe(ffprobe: str, path: Path) -> tuple[int, int, int, bool]:
    """Return ``(width, height, duration_ms, has_video)``.

    ``has_video`` is what separates a real clip from an audio file with a .mp4 suffix;
    a plain "ffmpeg opened it" check would happily add a soundtrack to the library.
    """
    try:
        completed = subprocess.run(
            [ffprobe, *_PROBE_ARGS, str(path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AppError(
            "ffmpeg_missing",
            f"找不到 {ffprobe}",
            hint="安装 ffmpeg 并确保在 PATH 上，或设置 AM_FFPROBE_PATH。",
            status=503,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("asset_probe_timeout", "读取素材信息超时", status=504) from exc

    if completed.returncode != 0:
        raise AppError(
            "asset_unreadable",
            f"{path.name} 读不出来",
            hint=f"ffprobe 报错：{(completed.stderr or '').strip()[:300]}",
        )

    import json

    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        return 0, 0, 0, False

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    # A rotation tag swaps the reported axes. Reaching the same answer as every viewer
    # matters here: a sideways image in the library produces a sideways block.
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = abs(int(side["rotation"]))
    if rotation % 180 == 90:
        width, height = height, width

    raw_duration = payload.get("format", {}).get("duration") or video.get("duration") or 0
    try:
        duration_ms = round(float(raw_duration) * 1000)
    except (TypeError, ValueError):
        duration_ms = 0
    return width, height, max(0, duration_ms), True


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return KIND_IMAGE
    if suffix in VIDEO_SUFFIXES:
        return KIND_VIDEO
    return None


def import_asset(
    session: Session,
    settings: Settings,
    project_id: str,
    source: Path,
) -> VideoAsset:
    """Copy one image or clip into the project and register it.

    Importing identical bytes returns the existing row: the browser already holds URLs
    for it, and a duplicate would appear twice in the picker with no way to tell which
    one the timeline is using.
    """
    if session.get(Project, project_id) is None:
        raise AppError("project_not_found", f"项目 {project_id} 不存在", status=404)

    resolved = source.expanduser()
    if not resolved.is_file():
        raise AppError(
            "asset_file_missing",
            f"找不到 {source.name}",
            hint="确认文件路径。",
            status=404,
        )
    kind = _kind_of(resolved)
    if kind is None:
        raise AppError(
            "asset_unsupported",
            f"不支持这种文件：{resolved.suffix or '（没有扩展名）'}",
            hint="图片用 png/jpg/webp，视频用 mp4/mov/webm。",
        )

    size = resolved.stat().st_size
    if size > MAX_BYTES:
        raise AppError(
            "asset_too_large",
            f"{resolved.name} 有 {size / 1024 / 1024:.0f} MB，太大了",
            hint=f"素材上限 {MAX_BYTES // 1024 // 1024} MB。长视频请先剪短再导入。",
        )

    digest = sha256_of(resolved)
    existing = session.scalars(
        select(VideoAsset).where(
            VideoAsset.project_id == project_id, VideoAsset.sha256 == digest
        )
    ).first()
    if existing is not None:
        log.info("asset.reused", project=project_id, asset=existing.id, sha256=digest[:12])
        return existing

    width, height, duration_ms, has_video = _probe(settings.ffprobe_path, resolved)
    if not has_video:
        raise AppError(
            "asset_no_video",
            f"{resolved.name} 里没有画面",
            hint="这个文件只有声音。素材框需要图片或视频。",
        )

    target_dir = asset_dir(settings, project_id)
    # Named by hash so two `IMG_0001.png` from different folders cannot collide, and so
    # a shutil.copy that dies halfway cannot leave a plausible-looking partial file
    # under a name another row points at.
    target = target_dir / f"{digest[:16]}{resolved.suffix.lower()}"
    if not target.exists():
        shutil.copyfile(resolved, target)

    row = VideoAsset(
        id=new_id("vasset"),
        project_id=project_id,
        kind=kind,
        path=str(target),
        sha256=digest,
        width=width,
        height=height,
        duration_ms=duration_ms,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    log.info(
        "asset.imported",
        project=project_id,
        asset=row.id,
        kind=kind,
        width=width,
        height=height,
        duration_ms=duration_ms,
    )
    return row


def get(session: Session, asset_id: str) -> VideoAsset | None:
    return session.get(VideoAsset, asset_id)


def require(session: Session, asset_id: str) -> VideoAsset:
    row = session.get(VideoAsset, asset_id)
    if row is None:
        raise AppError("asset_not_found", "素材不存在", status=404)
    return row


def list_assets(session: Session, project_id: str) -> list[VideoAsset]:
    """Newest first.

    Ordered by ``created_at`` **then id**: the timestamp has one-second resolution, so two
    assets imported in the same second would otherwise come back in whatever order SQLite
    felt like, and the picker would reorder itself between two identical requests. The ids
    are time-sortable (``ids.new_id``), so they break the tie the way a human expects.
    """
    return list(
        session.scalars(
            select(VideoAsset)
            .where(VideoAsset.project_id == project_id)
            .order_by(VideoAsset.created_at.desc(), VideoAsset.id.desc())
        )
    )


def view_all(session: Session, settings: Settings, project_id: str) -> list[AssetView]:
    views: list[AssetView] = []
    for row in list_assets(session, project_id):
        path = Path(row.path)
        views.append(
            AssetView(
                row=row,
                url=f"/api/projects/{project_id}/video/assets/{row.id}/file",
                exists=path.is_file(),
            )
        )
    return views


def delete_asset(session: Session, asset_id: str) -> None:
    """Remove one asset and its file.

    Refused while a timeline block still points at it. The alternative — deleting the
    asset and blanking the block — leaves a block that silently shows nothing, and the
    user has no way to tell that from a rendering bug. Naming the block is what makes
    the message actionable.
    """
    from animatter.models import VideoItem

    row = session.get(VideoAsset, asset_id)
    if row is None:
        raise AppError("asset_not_found", "素材不存在", status=404)

    users = list(
        session.scalars(
            select(VideoItem).where(
                VideoItem.project_id == row.project_id, VideoItem.asset_id == asset_id
            )
        )
    )
    if users:
        first = users[0]
        raise AppError(
            "asset_in_use",
            f"这个素材还用在 {len(users)} 个素材块里",
            hint=(
                f"先删掉 {first.start_ms / 1000:.0f}s 那个块（或者换成别的素材），再删素材。"
            ),
            status=409,
        )

    path = Path(row.path)
    session.delete(row)
    session.flush()
    path.unlink(missing_ok=True)
    log.info("asset.deleted", asset=asset_id)


def file_path(session: Session, project_id: str, asset_id: str) -> Path:
    """The file to serve, with both the project and existence checked.

    Looks the row up **by project** as well as by id: the route is project-scoped, and a
    bare ``session.get`` would let one project serve another's material to anyone who
    guessed an id.
    """
    row = session.get(VideoAsset, asset_id)
    if row is None or row.project_id != project_id:
        raise AppError("asset_not_found", "素材不存在", status=404)
    path = Path(row.path)
    if not path.is_file():
        raise AppError(
            "asset_file_missing",
            "素材文件不见了",
            hint="重新导入这个素材。",
            status=404,
        )
    return path
