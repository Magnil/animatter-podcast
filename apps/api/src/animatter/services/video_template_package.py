"""``.template`` packages: a compiled template that survives leaving this machine.

Why a package exists
--------------------
A compiled template is a directory of PNGs plus one row of JSON, keyed by the PSD's hash and
living inside the project that imported it. That is the right shape for "this project's
template", and the wrong shape for "give this to someone else": the art names are absolute
paths on the machine that compiled them, and the fonts are whatever happened to be installed.

The package is a zip with a different extension:

    manifest.json          schema version, canvas, box, fit, slots, art rects, file hashes
    art/*.png              every compiled PNG, flat, under the name the build used
    fonts/*.ttf            the fonts the slots ask for, when they are not OS fonts

Not in the package, deliberately: ``edits_json`` (this project's title and photos) and
``bindings_json`` (speaker ids that mean nothing in another project). A template is a
*poster and its slots*; the content on it belongs to whoever imported it. Getting this
backwards is how importing a template would silently replace a film's title.

Security
--------
This is the only route that turns bytes from outside into files on disk, so extraction is
written out rather than delegating to ``ZipFile.extractall``: every member name is checked
(absolutes, drive letters, ``..``), the type is checked (no symlinks, no nested zips), and
both per-file and total sizes are capped before anything is written.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import fonts
from animatter.models import Project, VideoTemplate
from animatter.services import video_templates as template_service

log = get_logger(__name__)

#: Bumped when the manifest shape changes in a way an older reader cannot honour. The reader
#: refuses a newer number instead of guessing: a half-understood template compiles into a
#: film with the wrong layout, which is worse than a clear "upgrade the app".
SCHEMA = 1

#: Extensions a package may carry. PNG for art, TTF/OTF/TTC for fonts, JSON for the manifest.
_ALLOWED_SUFFIXES = frozenset({".json", ".png", ".ttf", ".ttc", ".otf"})

#: Caps. A template is ~1.2 MB of art plus at most a couple of CJK font files; 64 MB is
#: already two orders of magnitude of headroom, so hitting it means the file is not a template.
MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_FILES = 200

#: Where an imported package lands, relative to the project's templates directory.
PACKAGE_SUBDIR = "packages"

#: The package every new project starts from. Shipped as real package data rather than built
#: at install time: a default assembled on first use would be a *second* producer of the format,
#: and it would be built from whatever happened to be on that machine.
BUNDLED = Path(__file__).resolve().parents[1] / "defaults" / "default.template"

#: Avatar slot fields that name a PNG inside the build directory. Rewritten to bare file names
#: on export and back to absolute paths on import — the same trick ``asset_path`` relies on.
_ART_FIELDS = ("lit_path", "dim_path", "frame_path", "mask_path")


@dataclass(frozen=True)
class PackageInfo:
    """What a package says about itself, once read and validated."""

    name: str
    canvas_w: int
    canvas_h: int
    box_rect: dict[str, int]
    box_fit_default: str
    slots: dict[str, Any]
    art: dict[str, Any]
    files: dict[str, bytes]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slot_art(value: object, base: Path) -> Path | None:
    """Resolve one ``slots_json`` art path, which is stored in one of three shapes.

    ``compile_template`` writes ``data\\projects\\<id>\\video\\templates\\build-x\\avatar0_lit.png``
    — relative to the **process cwd**, because ``Settings.data_dir`` is a relative path. An
    imported package writes the same thing (its target is under the same ``data`` tree), and a
    hand-edited or test fixture may write a bare file name. Trying "as given" first and then
    "next to the poster" covers all three; resolving only against the poster silently dropped
    every avatar from every exported package, because the joined path never existed.
    """
    text = str(value or "")
    if not text:
        return None
    given = Path(text)
    nearby = base / given.name
    # An absolute path is what a test fixture and a hand-edited row write; the compiled row
    # writes cwd-relative. Try the intended meaning first, then the other reading — a stale
    # absolute path pointing at an older build directory is more likely than a genuine
    # cross-directory reference.
    candidates = (given, nearby) if given.is_absolute() else (nearby, given)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _copied_art(row: VideoTemplate) -> list[Path]:
    """Every compiled PNG this template's slots point at, plus the poster and the artwork.

    Replacement photos under ``edits/`` are **not** included: they are this project's content,
    and the package is supposed to be the designer's poster. A slot whose photo was replaced
    therefore travels with the PSD's own photo, which is exactly what "export the template"
    should mean.
    """
    base = Path(row.background_path).parent
    out: list[Path] = []
    for name in (
        "background.png",
        "background_bare.png",
        "background_open.png",
        "background_bare_open.png",
        "plate.png",
        "overlay.png",
    ):
        candidate = base / name
        if candidate.is_file():
            out.append(candidate)
    slots = json.loads(row.slots_json or "{}")
    for slot in slots.get("avatars") or []:
        if not isinstance(slot, dict):
            continue
        for field in _ART_FIELDS:
            art = _slot_art(slot.get(field), base)
            if art is not None:
                out.append(art)
    # De-duplicate by name: two avatar slots may share a frame PNG, and a zip with two
    # identical entries is a warning a reader has to think about for no benefit.
    seen: dict[str, Path] = {}
    for path in out:
        seen.setdefault(path.name, path)
    return [seen[name] for name in sorted(seen)]


def _portable_slots(row: VideoTemplate) -> dict[str, Any]:
    """``slots_json`` with every art path reduced to a bare file name.

    Also drops the ``content`` text roles (§19.37 ⑦): they never render, and a slot a user can
    see but not affect is a slot that reads as broken.
    """
    slots = json.loads(row.slots_json or "{}")
    out: dict[str, Any] = json.loads(json.dumps(slots))  # deep copy, keeps JSON types
    texts = [
        item for item in out.get("texts") or [] if isinstance(item, dict) and item.get("role") != "content"
    ]
    out["texts"] = texts
    for slot in out.get("avatars") or []:
        if not isinstance(slot, dict):
            continue
        for field in _ART_FIELDS:
            value = str(slot.get(field) or "")
            if value:
                slot[field] = Path(value).name
    return out


def _fonts_used(slots: dict[str, Any]) -> list[str]:
    """Families the slots ask for, in first-seen order.

    "Asked for" is every slot's ``font`` field after ``content`` slots are dropped — a text
    slot whose family cannot be resolved is not an export error here; it fails loudly at
    render time with the list of what *is* installed, which is the better message anyway.
    """
    wanted: list[str] = []
    for slot in slots.get("texts") or []:
        if not isinstance(slot, dict):
            continue
        family = str(slot.get("font") or "").strip()
        if family and family not in wanted:
            wanted.append(family)
    return wanted


def _is_system_font(face: fonts.FontFace) -> bool:
    """True for a font the OS ships, which the package has no business copying.

    Deliberately **not** including the per-user font directory: 「Baigo」 and 「Alimama
    ShuHeiTi」 are installed by the designer, not by Windows, and a template that needs them is
    exactly the case a package exists for. The line is "we ship what you installed, not what
    the OS ships" — copying ``msyh.ttc`` (15 MB, licensed to the OS) into every template would
    be neither useful nor ours to hand out.
    """
    try:
        resolved = face.path.resolve()
    except OSError:
        return True
    for folder in fonts.SYSTEM_DIRS:
        try:
            resolved.relative_to(folder.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def build_package(row: VideoTemplate, *, name: str) -> bytes:
    """Build the zip for one template row.

    Split out from :func:`export_package` so the **default template** is built by the very
    same code as a user's export. If the bundled default could only be produced by a script
    that knows more than the exporter does, then a user's package would be a different
    artifact than the one shipped — and the first thing to drift would be the format.

    Built in memory: a template is ~1.2 MB, and writing it to a temp file only to stream it
    back would add a cleanup path for no benefit.
    """
    if row.status != "ready" or not row.background_path:
        raise AppError(
            "template_not_ready",
            "模板还没编译好，先等编译完成再导出",
            status=409,
        )

    art_files = _copied_art(row)
    if not art_files:
        raise AppError(
            "template_art_missing",
            "模板的图不在磁盘上了",
            hint="删掉这个模板重新导入一次。",
            status=409,
        )
    slots = _portable_slots(row)
    art = slots.get("art")
    if not isinstance(art, dict) or not art:
        raise AppError(
            "template_not_split",
            "这个模板是旧格式，没有底板和描边的独立图",
            hint="先「重新编译」一次，再导出模板。",
            status=409,
        )

    files: dict[str, bytes] = {}
    for path in art_files:
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise AppError(
                "template_art_too_large",
                f"{path.name} 太大（{len(data) // 1024} KB），不像模板里的一张图",
                status=409,
            )
        files[f"art/{path.name}"] = data

    bundled: list[dict[str, str]] = []
    seen_families: set[str] = set()
    for family in _fonts_used(slots):
        face = fonts.resolve(family)
        if face is None or _is_system_font(face) or family in seen_families:
            continue
        try:
            data = face.path.read_bytes()
        except OSError:
            log.warning("template.font_unreadable", family=family, path=str(face.path))
            continue
        if len(data) > MAX_FILE_BYTES:
            log.warning("template.font_too_large", family=family, size=len(data))
            continue
        seen_families.add(family)
        member = f"fonts/{face.path.name}"
        files[member] = data
        bundled.append({"family": family, "file": member, "sha256": _digest(data)})

    manifest = {
        "schema": SCHEMA,
        "name": name,
        "created_at": utcnow_iso(),
        "canvas": {"w": row.canvas_w, "h": row.canvas_h},
        "box_rect": json.loads(row.box_rect or "{}"),
        "box_fit_default": row.box_fit_default,
        "slots": slots,
        "art": art,
        "fonts": bundled,
        "files": {name: _digest(data) for name, data in files.items()},
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    log.info(
        "template.package_built",
        template=row.id,
        files=len(files),
        fonts=len(bundled),
        bytes=len(buffer.getvalue()),
    )
    return buffer.getvalue()


def export_package(session: Session, settings: Settings, project_id: str) -> tuple[str, bytes]:
    """The zip for one project's template, named after the project."""
    row = template_service.require(session, project_id)
    name = _package_name(session, project_id)
    return f"{name}.template", build_package(row, name=name)


def _package_name(session: Session, project_id: str) -> str:
    """The zip's own name: the project's, cleaned for a filename.

    Not "template" — a user exporting from three projects ends up with
    ``下载/template.template``, ``下载/template(1).template``, and no way to tell them apart.
    """
    project = session.get(Project, project_id)
    raw = (project.name if project is not None else "") or "模板"
    cleaned = "".join(char for char in raw if char not in '\\/:*?"<>|').strip()
    return cleaned[:60] or "模板"


def read_package(data: bytes) -> PackageInfo:
    """Validate a package and return everything in it, or raise with the reason.

    Reads into memory rather than extracting to a temp directory first: validation has to be
    able to say "no" before a single byte lands under ``data/``, and 64 MB is a bound the
    process can afford.
    """
    if len(data) > MAX_BYTES:
        raise AppError(
            "template_package_too_large",
            f"模板包超过 {MAX_BYTES // 1024 // 1024} MB",
            hint="模板包通常 1–3 MB，这个文件可能不是模板。",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AppError(
            "template_package_invalid",
            "这个文件不是有效的模板包",
            hint="模板包是扩展名改成 .template 的 zip，直接改后缀的 PSD 读不了。",
        ) from exc

    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members:
            raise AppError("template_package_empty", "模板包是空的")
        if len(members) > MAX_FILES:
            raise AppError(
                "template_package_too_many_files",
                f"模板包里有 {len(members)} 个文件，超过上限 {MAX_FILES}",
            )

        files: dict[str, bytes] = {}
        total = 0
        for item in members:
            name = _safe_name(item.filename)
            suffix = Path(name).suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES:
                raise AppError(
                    "template_package_type",
                    f"模板包里不该有 {name}",
                    hint="模板包只放 PNG、字体文件和 manifest.json。",
                )
            if item.file_size > MAX_FILE_BYTES:
                raise AppError(
                    "template_package_file_too_large",
                    f"{name} 超过 {MAX_FILE_BYTES // 1024 // 1024} MB",
                )
            total += item.file_size
            if total > MAX_BYTES:
                raise AppError(
                    "template_package_too_large",
                    f"模板包解开后超过 {MAX_BYTES // 1024 // 1024} MB",
                )
            with archive.open(item) as handle:
                files[name] = handle.read()

    raw_manifest = files.get("manifest.json")
    if raw_manifest is None:
        raise AppError(
            "template_package_no_manifest",
            "模板包里没有 manifest.json",
            hint="这可能是一个普通的 zip，不是本程序导出的模板。",
        )
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError(
            "template_package_manifest",
            "manifest.json 读不出来",
            hint="模板包可能被编辑器改坏了。",
        ) from exc
    if not isinstance(manifest, dict):
        raise AppError("template_package_manifest", "manifest.json 不是一个对象")

    schema = manifest.get("schema")
    if not isinstance(schema, int) or schema < 1:
        raise AppError("template_package_manifest", "manifest.json 里没有版本号")
    if schema > SCHEMA:
        raise AppError(
            "template_package_newer",
            f"这个模板包是更新版本导出的（schema {schema}）",
            hint="把本程序升级之后再导入。",
            status=409,
        )

    _check_hashes(manifest, files)

    canvas = manifest.get("canvas") or {}
    box = manifest.get("box_rect") or {}
    slots = manifest.get("slots") or {}
    art = manifest.get("art") or {}
    if not isinstance(canvas, dict) or not isinstance(box, dict) or not isinstance(slots, dict):
        raise AppError("template_package_manifest", "manifest.json 的画布或框字段格式不对")
    width = int(canvas.get("w") or 0)
    height = int(canvas.get("h") or 0)
    if width < 320 or height < 180:
        raise AppError(
            "template_package_manifest",
            f"manifest.json 里的画布是 {width}×{height}，不像一个模板",
        )

    for role in ("plate", "stroke"):
        entry = art.get(role) if isinstance(art, dict) else None
        if not isinstance(entry, dict):
            raise AppError(
                "template_package_incomplete",
                "模板包里没有底板或描边的记录",
                hint="用第 137 轮之后导出的模板包；更早的模板还没有拆分。",
            )
        art_name = entry.get("path")
        if not isinstance(art_name, str) or f"art/{Path(art_name).name}" not in files:
            raise AppError(
                "template_package_incomplete",
                f"模板包说它有 {art_name}，但包里没有这张图",
            )

    return PackageInfo(
        name=str(manifest.get("name") or "模板"),
        canvas_w=width,
        canvas_h=height,
        box_rect={axis: int(box.get(axis) or 0) for axis in ("x", "y", "w", "h")},
        box_fit_default=str(manifest.get("box_fit_default") or "blur_fill"),
        slots=slots,
        art=art,
        files=files,
    )


def _safe_name(name: str) -> str:
    """Reject anything that would write outside the destination directory.

    Checked on the *raw* member name, not a normalised one: ``zipfile`` already strips drive
    letters and leading separators when building ``.filename``, so a name that still contains
    a ``..`` component or an absolute path here really did come that way.
    """
    cleaned = name.replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        raise AppError(
            "template_package_unsafe",
            f"模板包里有一个不该出现的路径：{name}",
            hint="这个文件可能不是本程序导出的。",
        )
    if ":" in cleaned.split("/")[0]:
        raise AppError(
            "template_package_unsafe",
            f"模板包里有一个不该出现的路径：{name}",
        )
    return cleaned


def _check_hashes(manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    """Every file the manifest names must be present and match its hash.

    Not paranoia: a truncated download and a hand-edited manifest are the two ways a package
    arrives wrong, and both produce a *rendering* template with a subtly wrong poster. The
    hash turns that into a refusal at import time.
    """
    recorded = manifest.get("files")
    if not isinstance(recorded, dict) or not recorded:
        return
    for name, expected in recorded.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            continue
        data = files.get(_safe_name(name))
        if data is None:
            raise AppError(
                "template_package_incomplete",
                f"模板包里缺 {name}",
                hint="重新导出一份，或确认下载没有中断。",
            )
        if _digest(data) != expected:
            raise AppError(
                "template_package_corrupt",
                f"{name} 的内容和记录的不一致",
                hint="文件在传输中损坏了，重新导出一份。",
            )


def import_package(session: Session, settings: Settings, project_id: str, data: bytes) -> VideoTemplate:
    """Install a package as this project's template, replacing any existing one.

    Replaces rather than merges: a template is one coherent poster, and "keep the old title on
    the new artwork" is not a thing anyone wants. The old build directory is left on disk (a
    render may still be reading it, and the same rule applies to re-imports of a PSD).
    """
    if session.get(Project, project_id) is None:
        raise AppError("project_not_found", f"项目 {project_id} 不存在", status=404)
    info = read_package(data)

    stamp = _digest(data)[:12]
    target = template_service.templates_dir(settings, project_id) / PACKAGE_SUBDIR / stamp
    target.mkdir(parents=True, exist_ok=True)
    for name, payload in info.files.items():
        if name == "manifest.json":
            continue
        destination = target / Path(name).name
        destination.write_bytes(payload)

    _install_fonts(settings, info)

    slots = json.loads(json.dumps(info.slots))
    for slot in slots.get("avatars") or []:
        if not isinstance(slot, dict):
            continue
        for field in _ART_FIELDS:
            value = str(slot.get(field) or "")
            if value:
                slot[field] = str(target / Path(value).name)
    # A package from an older build may still carry `content` slots; they never render (§19.37 ⑦).
    slots["texts"] = [
        item
        for item in slots.get("texts") or []
        if not (isinstance(item, dict) and item.get("role") == "content")
    ]

    row = template_service.get(session, project_id)
    if row is None:
        row = VideoTemplate(
            id=new_id("vtpl"),
            project_id=project_id,
            created_at=utcnow_iso(),
        )
        session.add(row)
    row.source_path = str(target / "manifest.json")
    row.source_sha256 = stamp
    row.canvas_w = info.canvas_w
    row.canvas_h = info.canvas_h
    row.background_path = str(_poster_target(target))
    row.overlay_path = str(_art_target(target, info, "stroke", "overlay.png"))
    row.box_rect = json.dumps(info.box_rect)
    row.box_fit_default = (
        info.box_fit_default if info.box_fit_default in template_service.FITS else "blur_fill"
    )
    row.slots_json = json.dumps(slots, ensure_ascii=False)
    # Imported art is already compiled, so there is nothing to wait for. The manifest's word is
    # taken for the geometry — that is what the hashes are checked for.
    row.status = "ready"
    row.error = ""
    row.compiled_at = utcnow_iso()
    session.flush()
    log.info(
        "template.package_imported",
        project=project_id,
        template=row.id,
        dir=str(target),
        files=len(info.files),
    )
    return row


def _install_fonts(settings: Settings, info: PackageInfo) -> None:
    """Copy the package's fonts where ``fonts.discover()`` will find them.

    Not into the template directory: a font has to be discoverable by **family** from every
    code path that draws text (``_fit``, ``_face``, the subtitle ASS writer), and threading a
    per-template font path through those would be a much larger change than dropping the file
    in one shared directory. The trade-off is that a font imported once stays importable —
    which is what "installed" means.
    """
    fonts_dir = settings.fonts_dir
    copied = 0
    for name, payload in info.files.items():
        if not name.startswith("fonts/"):
            continue
        fonts_dir.mkdir(parents=True, exist_ok=True)
        destination = fonts_dir / Path(name).name
        if destination.is_file() and destination.stat().st_size == len(payload):
            continue
        destination.write_bytes(payload)
        copied += 1
    if copied:
        fonts.set_private_dir(fonts_dir)
        log.info("template.fonts_installed", count=copied, dir=str(fonts_dir))


def _poster_target(target: Path) -> Path:
    """The poster to draw first: the flattened one, or the open twin if that is all there is.

    ``background.png`` is the poster **with** the PSD's avatars flattened in, which is what a
    project with no bindings shows. A package made by a designer who never flattened anyone
    (or by this app's own default builder) may only carry ``background_open.png`` — same
    picture minus the plate, which is drawn as its own layer anyway.
    """
    for name in ("background.png", "background_open.png"):
        candidate = target / name
        if candidate.is_file():
            return candidate
    return target / "background.png"


def _art_target(target: Path, info: PackageInfo, key: str, fallback: str) -> Path:
    """Where the package put one artwork, preferring what its manifest recorded."""
    entry = info.art.get(key)
    name = entry.get("path") if isinstance(entry, dict) else None
    if isinstance(name, str) and (target / Path(name).name).is_file():
        return target / Path(name).name
    return target / fallback


def package_path_of(row: VideoTemplate) -> Path | None:
    """The directory an imported package lives in, when this template came from one."""
    if not row.source_path:
        return None
    manifest = Path(row.source_path)
    return manifest.parent if manifest.name == "manifest.json" else None


def default_package() -> bytes:
    """The bundled default template's bytes.

    A missing file is a broken installation, not a user error: say so with a code the UI can
    show as 「重装一次」 instead of a stack trace.
    """
    try:
        return BUNDLED.read_bytes()
    except OSError as exc:
        raise AppError(
            "template_default_missing",
            "程序自带的默认模板找不到了",
            hint="重新安装或更新一次本程序。",
            status=500,
        ) from exc


def install_default(session: Session, settings: Settings, project_id: str) -> VideoTemplate:
    """Give a project the bundled default template.

    Same code path as a user's import, deliberately: if the default could only be installed by
    a special case, then the default would be the one package nothing ever exercised.
    """
    row = import_package(session, settings, project_id, default_package())
    log.info("template.default_installed", project=project_id, template=row.id)
    return row
