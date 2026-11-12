"""The asset library: import, dedupe, and the refusals that matter (§19.4).

Assets are **copied** into the project, not referenced. That single decision is what makes
a project portable and what makes these tests able to check something real: the stored file
must exist after the source is deleted.

The refusals are the interesting half. Every one of them exists because the alternative
fails *later* — during an export, after minutes of encoding, with a message about a file
the user does not remember choosing.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.errors import AppError
from animatter.ids import utcnow_iso
from animatter.models import VideoItem
from animatter.services import video_assets as asset_service


def image_at(tmp_path: Path, name: str = "poster.png", size: tuple[int, int] = (1080, 1920)) -> Path:
    path = tmp_path / name
    Image.new("RGB", size, (40, 80, 200)).save(path)
    return path


@pytest.fixture()
def project(session: Session) -> str:
    version = seed_version(session, text="剧集标签")
    session.commit()
    return version.project_id


@pytest.fixture()
def app_settings(client: TestClient) -> object:
    # Named `app_settings` and not `settings`: conftest already has a `settings`
    # fixture that `client` depends on, so reusing the name makes the dependency
    # recursive and every test in the file errors at setup.
    return client.app.state.settings  # type: ignore[attr-defined]


# --- import -------------------------------------------------------------


def test_an_image_is_registered_with_its_real_size(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    assert row.kind == "image"
    assert (row.width, row.height) == (1080, 1920)
    assert row.duration_ms == 0


def test_the_file_is_copied_into_the_project(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """A project that points at the user's Downloads folder works until it does not."""
    source = image_at(tmp_path)
    row = asset_service.import_asset(session, app_settings, project, source)
    stored = Path(row.path)
    assert stored.is_file()
    assert stored != source

    source.unlink()
    assert stored.is_file(), "the copy must survive deleting the original"


def test_the_copy_is_named_by_hash(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """Two different ``IMG_0001.png`` must not collide."""
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    assert Path(row.path).stem == row.sha256[:16]


def test_the_original_name_survives_the_hash_rename(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """The file on disk is named by hash; the *label* is what the user chose.

    Round 104: the library showed ``b825e065e6cf9826.png`` for every imported still, so two
    of them were indistinguishable in the picker and on the timeline.
    """
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path, "开场封面.png"))
    session.commit()
    assert Path(row.path).stem != "开场封面"
    assert row.original_name == "开场封面.png"
    view = next(
        item for item in asset_service.view_all(session, app_settings, project) if item.row.id == row.id
    )
    assert view.name == "开场封面.png"


def test_reimporting_the_same_bytes_reuses_the_row(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    first = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    second = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    assert first.id == second.id


def test_a_jpeg_is_accepted(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    row = asset_service.import_asset(
        session, app_settings, project, image_at(tmp_path, "poster.jpg")
    )
    assert row.kind == "image"


def test_a_video_is_registered_with_its_duration(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """A real encode: ffprobe must report a video stream *and* a length."""
    clip = tmp_path / "clip.mp4"
    _ = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=25:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(clip),
            "-y",
        ],
        capture_output=True,
        check=False,
    )
    if not clip.is_file():
        pytest.skip("这台机器上没有可用的 ffmpeg 来生成测试视频")

    row = asset_service.import_asset(session, app_settings, project, clip)
    assert row.kind == "video"
    assert row.duration_ms > 2000
    assert (row.width, row.height) == (320, 180)


# --- refusals -----------------------------------------------------------


def test_a_missing_file_names_it(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    with pytest.raises(AppError) as caught:
        asset_service.import_asset(session, app_settings, project, tmp_path / "nope.png")
    assert "nope.png" in caught.value.message


def test_an_unsupported_suffix_is_refused(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    weird = tmp_path / "notes.txt"
    weird.write_text("hello", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        asset_service.import_asset(session, app_settings, project, weird)
    assert caught.value.code == "asset_unsupported"


def test_an_audio_file_with_a_video_suffix_is_refused(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """``ffmpeg`` opens a truncated mp4; that does not make it a picture."""
    broken = tmp_path / "sound.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    with pytest.raises(AppError) as caught:
        asset_service.import_asset(session, app_settings, project, broken)
    assert caught.value.code in {"asset_unreadable", "asset_no_video"}


def test_an_oversized_file_is_refused_before_copying(
    session: Session, app_settings: object, project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = image_at(tmp_path)
    monkeypatch.setattr(asset_service, "MAX_BYTES", 10)
    with pytest.raises(AppError) as caught:
        asset_service.import_asset(session, app_settings, project, source)
    assert caught.value.code == "asset_too_large"


# --- listing and serving ------------------------------------------------


def test_listing_is_newest_first(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    first = asset_service.import_asset(session, app_settings, project, image_at(tmp_path, "a.png"))
    second = asset_service.import_asset(
        session, app_settings, project, image_at(tmp_path, "b.png", (800, 600))
    )
    ids = [view.row.id for view in asset_service.view_all(session, app_settings, project)]
    assert ids.index(second.id) < ids.index(first.id)


def test_the_url_is_project_scoped(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    view = asset_service.view_all(session, app_settings, project)[0]
    assert f"/api/projects/{project}/video/assets/" in view.url


def test_a_missing_file_is_flagged_rather_than_hidden(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """The picker has to be able to draw a broken asset instead of pretending it is fine."""
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    Path(row.path).unlink()
    view = asset_service.view_all(session, app_settings, project)[0]
    assert view.exists is False


def test_serving_another_projects_asset_is_refused(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """A bare ``session.get`` would let anyone with a guessed id read the file."""
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    other = seed_version(session, text="别的项目")
    session.commit()
    with pytest.raises(AppError) as caught:
        asset_service.file_path(session, other.project_id, row.id)
    assert caught.value.code == "asset_not_found"


# --- deletion -----------------------------------------------------------


def test_deleting_removes_the_row_and_the_file(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    stored = Path(row.path)
    asset_service.delete_asset(session, row.id)
    assert not stored.exists()


def test_deleting_is_refused_while_a_block_uses_it(
    session: Session, app_settings: object, project: str, tmp_path: Path
) -> None:
    """Blanking the block instead would leave a block that silently shows nothing."""
    row = asset_service.import_asset(session, app_settings, project, image_at(tmp_path))
    session.add(
        VideoItem(
            id="vitem_x",
            project_id=project,
            kind="media",
            start_ms=0,
            end_ms=1000,
            z=0,
            fit="cover",
            motion="none",
            asset_id=row.id,
            deck_id=None,
            slide_id=None,
            gallery_json="[]",
            orphaned=False,
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
    )
    session.flush()

    with pytest.raises(AppError) as caught:
        asset_service.delete_asset(session, row.id)
    assert caught.value.code == "asset_in_use"
    assert "0s" in caught.value.hint


# --- HTTP ---------------------------------------------------------------


def test_the_upload_endpoint_accepts_bytes(
    client: TestClient, session: Session, project: str, tmp_path: Path
) -> None:
    """Drag-and-drop uploads, because a browser ``File`` has no path."""
    source = image_at(tmp_path, "dropped.png")
    response = client.post(
        f"/api/projects/{project}/video/assets/upload",
        files={"file": ("dropped.png", source.read_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "image"
    assert body["width"] == 1080


def test_the_upload_endpoint_rejects_an_extensionless_file(
    client: TestClient, project: str
) -> None:
    response = client.post(
        f"/api/projects/{project}/video/assets/upload",
        files={"file": ("mystery", b"\x89PNG\r\n", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "asset_unsupported"


def test_the_upload_endpoint_names_the_asset_with_the_browsers_file_name(
    client: TestClient, project: str
) -> None:
    """The bytes are staged as ``upload.png``; the label must not follow that.

    A ``File`` from a browser has no path, so the server writes it to a temp file whose name
    it chose. Reading the display name off *that* would call every uploaded asset 「upload」.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (640, 360), (200, 40, 40)).save(buffer, format="PNG")
    response = client.post(
        f"/api/projects/{project}/video/assets/upload",
        files={"file": ("嘉宾照片.png", buffer.getvalue(), "image/png")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "嘉宾照片.png"


def test_the_import_endpoint_takes_a_path(
    client: TestClient, project: str, tmp_path: Path
) -> None:
    source = image_at(tmp_path, "by-path.png")
    response = client.post(
        f"/api/projects/{project}/video/assets", json={"path": str(source)}
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "by-path.png"


def test_the_file_endpoint_serves_the_bytes(
    client: TestClient, project: str, tmp_path: Path
) -> None:
    source = image_at(tmp_path)
    created = client.post(
        f"/api/projects/{project}/video/assets", json={"path": str(source)}
    ).json()

    response = client.get(created["url"])
    assert response.status_code == 200
    assert response.content[:4] == b"\x89PNG"


def test_deleting_through_http_is_a_204(
    client: TestClient, project: str, tmp_path: Path
) -> None:
    created = client.post(
        f"/api/projects/{project}/video/assets", json={"path": str(image_at(tmp_path))}
    ).json()
    response = client.delete(f"/api/video/assets/{created['id']}")
    assert response.status_code == 204
