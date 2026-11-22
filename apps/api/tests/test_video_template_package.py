"""``.template`` packages: what travels, what is refused, and what does not come back (§19.36).

A package is the only path in this app that turns bytes from outside into files on disk, so the
tests are shaped the same way the reader is: the happy round trip, then one case per refusal.
Each refusal has a *specific* failure it prevents, which is why the assertions check error codes
rather than just "it raised":

- a zip bomb is a 64 MB cap on the **uncompressed** total, checked before reading,
- ``..`` in a member name is a write outside the destination,
- a newer ``schema`` is a template this build would half-understand,
- a hash mismatch is a truncated download that would otherwise render a subtly wrong poster,
- ``edits_json``/``bindings_json`` staying behind is what stops "import a template" from
  silently overwriting the film's title with the previous owner's.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.media import fonts
from animatter.models import VideoTemplate
from animatter.services import video_template_package as package_service

CANVAS = (1920, 1080)
BOX = {"x": 240, "y": 209, "w": 1440, "h": 810}
PLATE_RECT = {"x": 238, "y": 206, "w": 1444, "h": 815}
STROKE_RECT = {"x": 231, "y": 198, "w": 1456, "h": 829}


def write_png(path: Path, size: tuple[int, int] = CANVAS, colour=(20, 20, 20, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, colour).save(path)
    return path


@pytest.fixture()
def project_id(session: Session) -> str:
    version = seed_version(session, text="模板包")
    session.commit()
    return version.project_id


def make_template(session: Session, project_id: str, build: Path) -> VideoTemplate:
    """A template row whose build directory holds everything an export reads."""
    build.mkdir(parents=True, exist_ok=True)
    write_png(build / "background.png")
    write_png(build / "background_bare.png")
    write_png(build / "background_open.png")
    write_png(build / "background_bare_open.png")
    write_png(build / "overlay.png", (1456, 829))
    write_png(build / "plate.png", (1444, 815), (244, 244, 244, 255))
    write_png(build / "avatar0_lit.png", (236, 249))
    write_png(build / "avatar0_dim.png", (236, 249))
    write_png(build / "avatar0_frame.png", (236, 249))
    write_png(build / "avatar0_mask.png", (236, 249))
    slots = {
        "avatars": [
            {
                "name": "头像1",
                "label_name": "主播1",
                "rect": {"x": 42, "y": 200, "w": 177, "h": 188},
                "photo": {"x": 42, "y": 200, "w": 177, "h": 188},
                "label": "主播1",
                "lit_path": str(build / "avatar0_lit.png"),
                "dim_path": str(build / "avatar0_dim.png"),
                "frame_path": str(build / "avatar0_frame.png"),
                "mask_path": str(build / "avatar0_mask.png"),
                "mask_rect": {"x": 17, "y": 29, "w": 202, "h": 202},
                "name_key": "avatar:头像1",
            }
        ],
        "texts": [
            {
                "key": "episode",
                "role": "episode",
                "label": "期号",
                "name": "EP.23",
                "rect": {"x": 1708, "y": 15, "w": 200, "h": 50},
                "text": "EP.**23**",
                "font_size": 69,
                "colour": "#1947ba",
                "font": "Baigo",
            },
            {
                "key": "note:框内",
                "role": "content",
                "label": "框内文字",
                "name": "框内",
                "rect": BOX,
                "text": "本期嘉宾",
                "font_size": 24,
                "colour": "#000000",
                "font": "Baigo",
            },
        ],
        "notes": [],
        "art": {
            "plate": {"path": "plate.png", "rect": PLATE_RECT},
            "stroke": {"path": "overlay.png", "rect": STROKE_RECT},
        },
    }
    row = VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path=str(build / "source.psd"),
        source_sha256="a" * 64,
        canvas_w=CANVAS[0],
        canvas_h=CANVAS[1],
        background_path=str(build / "background.png"),
        overlay_path=str(build / "overlay.png"),
        box_rect=json.dumps(BOX),
        box_fit_default="blur_fill",
        slots_json=json.dumps(slots, ensure_ascii=False),
        bindings_json=json.dumps({"头像1": 1}),
        edits_json=json.dumps({"texts": {"episode": {"text": "EP.99"}}, "photos": {}}),
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def export(session: Session, settings: Settings, project_id: str) -> tuple[str, bytes]:
    return package_service.export_package(session, settings, project_id)


def rewrapped(payload: bytes, **overrides: object) -> bytes:
    """The same package with its manifest changed — what a hand-edited or older zip looks like."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    manifest = json.loads(members["manifest.json"].decode("utf-8"))
    for key, value in overrides.items():
        manifest[key] = value
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# --- the round trip -----------------------------------------------------


def test_a_package_carries_art_slots_and_nothing_from_this_project(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    name, payload = export(session, settings, project_id)
    assert name.endswith(".template")

    info = package_service.read_package(payload)
    assert (info.canvas_w, info.canvas_h) == CANVAS
    assert info.box_rect == BOX
    # Art is stored by bare file name: an absolute path from the exporting machine means
    # nothing on the importing one.
    assert info.slots["avatars"][0]["lit_path"] == "avatar0_lit.png"
    # `content` slots never render (§19.37 ⑦); shipping one makes the importer's panel show a
    # field that cannot change the picture.
    assert [slot["key"] for slot in info.slots["texts"]] == ["episode"]
    # The project's own title override and speaker bindings stay behind.
    assert "edits_json" not in info.slots
    assert "bindings_json" not in info.slots
    assert info.art["plate"]["rect"] == PLATE_RECT


def test_avatar_art_travels_even_when_the_slot_path_is_relative(
    session: Session,
    settings: Settings,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile_template`` stores avatar paths **relative to the process cwd**, not absolute.

    ``Settings.data_dir`` is ``Path("data")``, so ``slots_json`` ends up holding
    ``data\\projects\\<id>\\...\\avatar0_lit.png``. Resolving that special case as "relative to
    the poster" — or joining it to the poster directory as if it were a bare name — produces a
    path that never exists, and the package then quietly ships **no avatars at all**: the export
    still succeeds, the manifest still lists the six shared images, and the imported template
    renders with empty slots. That is how this bug shipped the first time.
    """
    # ``monkeypatch.chdir`` is what makes the relative form expressible at all: pytest's
    # ``tmp_path`` usually lives on a different drive than the checkout, and no relative path
    # can cross drives. Chdir also reproduces the production condition exactly — the app runs
    # from the repository root and ``data_dir`` is the relative ``Path("data")``.
    monkeypatch.chdir(tmp_path)
    row = make_template(session, project_id, tmp_path / "build")
    slots = json.loads(row.slots_json)
    for slot in slots["avatars"]:
        for field in ("lit_path", "dim_path", "frame_path", "mask_path"):
            slot[field] = str(Path(slot[field]).relative_to(tmp_path))
    row.slots_json = json.dumps(slots, ensure_ascii=False)
    session.flush()

    _name, payload = export(session, settings, project_id)
    info = package_service.read_package(payload)
    carried = {Path(name).name for name in info.files}
    for kind in ("lit", "dim", "frame", "mask"):
        assert f"avatar0_{kind}.png" in carried, f"{kind} 没有进包"


def test_a_package_records_a_hash_for_every_file_it_holds(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        names = {item.filename for item in archive.infolist()}
    assert set(manifest["files"]) == names - {"manifest.json"}
    assert all(len(digest) == 64 for digest in manifest["files"].values())


def test_exporting_a_template_that_predates_the_split_says_so(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    """An old row has a full-canvas overlay and no plate; a package of it would import as a
    template whose frame cannot move, which is precisely the thing packages are for."""
    row = make_template(session, project_id, tmp_path / "legacy")
    slots = json.loads(row.slots_json)
    slots.pop("art")
    row.slots_json = json.dumps(slots, ensure_ascii=False)
    session.flush()

    with pytest.raises(AppError) as caught:
        export(session, settings, project_id)
    assert caught.value.code == "template_not_split"


# --- import -------------------------------------------------------------


def test_importing_installs_a_ready_template_without_compiling(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    session.rollback()

    other = seed_version(session, text="另一个项目").project_id
    session.commit()
    row = package_service.import_package(session, settings, other, payload)
    session.commit()

    assert row.status == "ready"
    assert (row.canvas_w, row.canvas_h) == CANVAS
    target = Path(row.background_path).parent
    for name in ("background.png", "plate.png", "overlay.png", "avatar0_lit.png"):
        assert (target / name).is_file(), name
    # Slot paths are absolute again, and point into the new build directory.
    slots = json.loads(row.slots_json)
    assert Path(slots["avatars"][0]["lit_path"]).parent == target


def test_importing_twice_replaces_the_template_instead_of_stacking_rows(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    session.rollback()

    other = seed_version(session, text="另一个项目").project_id
    session.commit()
    first = package_service.import_package(session, settings, other, payload)
    session.commit()
    second = package_service.import_package(session, settings, other, payload)
    session.commit()
    assert first.id == second.id
    rows = session.query(VideoTemplate).filter(VideoTemplate.project_id == other).count()
    assert rows == 1


def test_a_package_that_is_not_a_zip_is_refused(
    session: Session, settings: Settings, project_id: str
) -> None:
    with pytest.raises(AppError) as caught:
        package_service.import_package(session, settings, project_id, b"not a zip")
    assert caught.value.code == "template_package_invalid"


def test_a_member_walking_out_of_the_directory_is_refused(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    """``..`` in a member name is the classic zip slip: it writes wherever it likes."""
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    members["art/../../escaped.png"] = b"\x89PNG\r\n\x1a\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(AppError) as caught:
        package_service.read_package(buffer.getvalue())
    assert caught.value.code == "template_package_unsafe"


def test_a_member_of_the_wrong_kind_is_refused(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    """A package is PNGs and fonts. An ``.exe`` in one means it came from somewhere else."""
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    members["art/tool.exe"] = b"MZ"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(AppError) as caught:
        package_service.read_package(buffer.getvalue())
    assert caught.value.code == "template_package_type"


def test_a_truncated_package_is_caught_by_its_hashes(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    """A download that stopped early is a zip that opens. Without hashes it imports a broken
    poster and the failure shows up as a mis-coloured frame minutes into a render."""
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    members["art/plate.png"] = members["art/plate.png"][:100]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(AppError) as caught:
        package_service.read_package(buffer.getvalue())
    assert caught.value.code == "template_package_corrupt"


def test_a_newer_schema_is_refused_rather_than_guessed(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with pytest.raises(AppError) as caught:
        package_service.read_package(rewrapped(payload, schema=package_service.SCHEMA + 1))
    assert caught.value.code == "template_package_newer"


def test_a_package_missing_its_plate_is_refused(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with pytest.raises(AppError) as caught:
        package_service.read_package(rewrapped(payload, art={"stroke": {"path": "overlay.png"}}))
    assert caught.value.code == "template_package_incomplete"


def test_a_zip_without_a_manifest_is_a_different_file(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        write_png(tmp_path / "x.png", (8, 8))
        archive.writestr("art/x.png", (tmp_path / "x.png").read_bytes())
    with pytest.raises(AppError) as caught:
        package_service.read_package(buffer.getvalue())
    assert caught.value.code == "template_package_no_manifest"


# --- fonts --------------------------------------------------------------


def test_an_installed_font_lands_where_discovery_looks(
    session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    """A template that needs 「Baigo」 has to work on a machine that never installed it.

    The bytes only have to land in ``data/fonts`` — validity is Pillow's business, and the
    directory is registered with ``media.fonts`` so the next walk finds it.
    """
    make_template(session, project_id, tmp_path / "build")
    _name, payload = export(session, settings, project_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    members["fonts/Package Only.ttf"] = b"\x00\x01\x00\x00" + b"\x00" * 64
    manifest = json.loads(members["manifest.json"].decode("utf-8"))
    manifest["files"]["fonts/Package Only.ttf"] = package_service._digest(
        members["fonts/Package Only.ttf"]
    )
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    session.rollback()
    other = seed_version(session, text="装字体").project_id
    session.commit()
    package_service.import_package(session, settings, other, buffer.getvalue())
    session.commit()

    installed = settings.fonts_dir / "Package Only.ttf"
    assert installed.is_file()


def test_the_private_font_directory_is_part_of_discovery(tmp_path: Path) -> None:
    """``set_private_dir`` is what makes ``data/fonts`` expensive to forget: without it a
    bundled font is on disk and invisible, which renders the film in a substituted face."""
    private = tmp_path / "fonts"
    private.mkdir()
    real = fonts.resolve("Baigo") or fonts.resolve("Arial")
    if real is None:
        pytest.skip("no font on this machine to copy")
    (private / real.path.name).write_bytes(real.path.read_bytes())

    fonts.set_private_dir(private)
    try:
        files = fonts._candidate_files()
        assert (private / real.path.name) in files
    finally:
        fonts.set_private_dir(None)


# --- endpoints ----------------------------------------------------------


def test_the_download_reports_a_filename_and_the_bytes(
    client: TestClient, session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    response = client.get(f"/api/projects/{project_id}/video/template/package")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert ".template" in response.headers["content-disposition"]
    assert int(response.headers["x-template-bytes"]) == len(response.content)
    assert zipfile.ZipFile(io.BytesIO(response.content)).read("manifest.json")


def test_uploading_a_package_installs_it(
    client: TestClient, session: Session, settings: Settings, project_id: str, tmp_path: Path
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    payload = client.get(f"/api/projects/{project_id}/video/template/package").content

    # A second project, so the import is not a no-op against the one that exported.
    other = seed_version(session, text="接收方").project_id
    session.commit()
    response = client.post(
        f"/api/projects/{other}/video/template/package",
        files={"file": ("模板.template", payload, "application/zip")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["canvas_w"] == CANVAS[0]
