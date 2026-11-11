"""Text and avatar edits over HTTP: fonts, overrides, replaced photos (§19.12).

The compile is the expensive part, so it happens once per module; everything here exercises
the *edit* path, which is what the UI calls. Each test asserts on the response body **and**
on the file the renderer will eventually read, because a green response with an untouched
PNG is exactly the failure this feature can produce.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from tests.test_corrections import seed_version

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_PSD = REPO_ROOT / "samples" / "播客模板.psd"


@pytest.fixture(scope="module")
def db_app(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    from animatter.config import Settings
    from animatter.main import create_app

    data_dir = tmp_path_factory.mktemp("edits") / "data"
    app = create_app(Settings(data_dir=data_dir, host="127.0.0.1", log_level="ERROR"))
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def imported(db_app: TestClient) -> tuple[str, str, dict[str, object]]:
    if not SAMPLE_PSD.is_file():
        pytest.skip("samples/播客模板.psd 不在仓库里")

    db = db_app.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = seed_version(session, text="这期聊聊罗小黑")
        project_id = version.project_id
        version_id = version.id
        session.commit()

    response = db_app.post(
        f"/api/projects/{project_id}/video/template", json={"path": str(SAMPLE_PSD)}
    )
    assert response.status_code == 201, response.text

    deadline = time.monotonic() + 120
    body: dict[str, object] = response.json()
    while time.monotonic() < deadline:
        body = db_app.get(f"/api/projects/{project_id}/video/template").json()
        if body["status"] not in {"pending", "compiling"}:
            break
        time.sleep(0.5)
    assert body["status"] == "ready", body
    return project_id, version_id, body


def _texts(body: dict[str, object]) -> list[dict[str, object]]:
    slots = body["slots"]
    assert isinstance(slots, dict)
    texts = slots["texts"]
    assert isinstance(texts, list)
    return [item for item in texts if isinstance(item, dict)]


def _by_role(body: dict[str, object], role: str) -> dict[str, object]:
    return next(item for item in _texts(body) if item.get("role") == role)


# --- fonts --------------------------------------------------------------


def test_the_font_list_is_served_without_an_explicit_call(db_app: TestClient) -> None:
    response = db_app.get("/api/fonts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["faces"] > 0
    assert isinstance(body["families"], list)
    assert len(body["families"]) == len(set(body["families"])), "duplicated families"


def test_the_design_fonts_are_listed_first_when_present(db_app: TestClient) -> None:
    body = db_app.get("/api/fonts").json()
    for family in body["preferred"]:
        assert family in body["families"], family


# --- text overrides -----------------------------------------------------


def test_the_title_slot_arrives_with_the_psd_defaults(
    imported: tuple[str, str, dict[str, object]],
) -> None:
    _project_id, _version_id, body = imported
    title = _by_role(body, "title")
    assert str(title["text"]).startswith("《罗小黑战记2》独家专访")
    assert title["font_size"] > 0
    assert str(title["colour"]).startswith("#")
    assert title["font"], "the slot must name a font, or the picker starts blank"


def test_editing_the_title_changes_the_response(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, _body = imported
    key = str(_by_role(_body, "title")["key"])

    response = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/{key}",
        json={"text": "新的标题"},
    )
    assert response.status_code == 200, response.text
    title = _by_role(response.json(), "title")
    assert title["text"] == "新的标题"
    # Everything else keeps the PSD's values: only what was sent changes.
    assert title["font_size"] == _by_role(_body, "title")["font_size"]

    restored = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/{key}", json={"text": ""}
    )
    assert restored.status_code == 200, restored.text
    assert str(_by_role(restored.json(), "title")["text"]).startswith("《罗小黑战记2》")


def test_the_font_and_size_can_be_overridden(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, body = imported
    key = str(_by_role(body, "episode")["key"])
    families = db_app.get("/api/fonts").json()["families"]
    pick = next((name for name in families if name != _by_role(body, "episode")["font"]), None)
    if pick is None:
        pytest.skip("only one family installed")

    response = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/{key}",
        json={"font": pick, "font_size": 80},
    )
    assert response.status_code == 200, response.text
    episode = _by_role(response.json(), "episode")
    assert episode["font"] == pick
    assert episode["font_size"] == 80

    db_app.patch(f"/api/projects/{project_id}/video/template/texts/{key}", json={"font_size": 69})
    db_app.patch(f"/api/projects/{project_id}/video/template/texts/{key}", json={"font": ""})


def test_an_unknown_slot_is_rejected_not_silently_stored(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, _body = imported
    response = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/note:不存在的图层",
        json={"text": "x"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "template_slot_unknown"


def test_a_missing_font_is_rejected_with_the_installed_list(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, body = imported
    key = str(_by_role(body, "title")["key"])
    response = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/{key}",
        json={"font": "绝对没有这个字体XYZ"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "font_missing"


def test_a_five_digit_font_size_is_refused_before_pillow_sees_it(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, body = imported
    key = str(_by_role(body, "title")["key"])
    response = db_app.patch(
        f"/api/projects/{project_id}/video/template/texts/{key}", json={"font_size": 99999}
    )
    assert response.status_code == 422


# --- avatar photos ------------------------------------------------------


def test_replacing_one_avatar_photo_rewrites_only_that_slot(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, body = imported
    slots = body["slots"]
    assert isinstance(slots, dict)
    avatars = slots["avatars"]
    assert isinstance(avatars, list)
    before = {str(slot["name"]): Path(str(slot["lit_path"])).read_bytes() for slot in avatars}

    photo = Path(db_app.app.state.settings.data_dir) / "green.png"
    Image.new("RGB", (300, 300), (0, 180, 0)).save(photo)
    created = db_app.post(
        f"/api/projects/{project_id}/video/assets", json={"path": str(photo)}
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]

    target = str(avatars[0]["name"])
    response = db_app.put(
        f"/api/projects/{project_id}/video/template/avatars/{target}/photo",
        json={"asset_id": asset_id},
    )
    assert response.status_code == 200, response.text
    updated = response.json()["slots"]["avatars"]
    replaced = next(slot for slot in updated if slot["name"] == target)
    assert replaced["photo_replaced"] is True
    assert Path(str(replaced["lit_path"])).read_bytes() != before[target]

    # The other three are untouched — a per-slot replacement, not a template-wide one.
    for slot in updated:
        if slot["name"] == target:
            continue
        assert Path(str(slot["lit_path"])).read_bytes() == before[str(slot["name"])]

    # And the served URL returns the replacement, not the PSD's face.
    served = db_app.get(str(replaced["lit_url"]))
    assert served.status_code == 200
    assert served.content == Path(str(replaced["lit_path"])).read_bytes()


def test_restoring_the_photo_puts_the_psd_face_back(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    """``asset_id: null`` drops the override; the compiled PNG is what shows again.

    Reads the template fresh instead of reusing the module fixture's snapshot. The fixture is
    captured before any of these tests run, and asserting "restored" against it would pass
    without the restore doing anything at all — which is precisely how this test failed the
    first time: it found no replaced slot, because it was looking at the original response.
    """
    project_id, _version_id, _body = imported
    current = db_app.get(f"/api/projects/{project_id}/video/template").json()
    replaced = next(slot for slot in current["slots"]["avatars"] if slot["photo_replaced"] is True)
    name = str(replaced["name"])
    bytecode_before = Path(str(replaced["lit_path"])).read_bytes()

    response = db_app.put(
        f"/api/projects/{project_id}/video/template/avatars/{name}/photo",
        json={"asset_id": None},
    )
    assert response.status_code == 200, response.text
    restored = next(item for item in response.json()["slots"]["avatars"] if item["name"] == name)
    assert restored["photo_replaced"] is False
    # Back to the compiled file, whose content is not the replacement we just read.
    assert Path(str(restored["lit_path"])).name.startswith("avatar")
    assert Path(str(restored["lit_path"])).read_bytes() != bytecode_before


def test_an_unknown_avatar_slot_is_rejected(
    db_app: TestClient, imported: tuple[str, str, dict[str, object]]
) -> None:
    project_id, _version_id, _body = imported
    response = db_app.put(
        f"/api/projects/{project_id}/video/template/avatars/头像9/photo",
        json={"asset_id": None},
    )
    assert response.status_code == 404
