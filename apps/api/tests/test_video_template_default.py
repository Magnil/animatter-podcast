"""The bundled default template, and the fact that a new project starts with one (§19.36).

What is pinned here is not "the default looks nice" — a test cannot say that. It is the three
properties that make a *bundled* default safe to hand every new project:

* the shipped bytes are a **real package**, built by the same ``build_package`` a user's export
  goes through, so the default cannot be a format nobody else produces;
* installing it is the same code path as a user's import (``install_default`` → ``import_package``),
  which is what stops the default from being the one package nothing exercises;
* a project is created even when the default cannot be installed — a convenience must never
  cost the user their project.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.models import VideoTemplate
from animatter.services import projects as project_service
from animatter.services import video_template_package as package_service


def test_the_shipped_default_is_a_readable_package() -> None:
    """The file in the source tree has to be a package this build can install verbatim."""
    info = package_service.read_package(package_service.default_package())
    assert info.canvas_w == 1920
    assert info.canvas_h == 1080
    assert set(info.art) == {"plate", "stroke"}
    # Four avatar slots and the seven slots that actually draw: 期号 / 标题 / 4 个名字 / 页脚.
    assert len(info.slots["avatars"]) == 4
    roles = [slot["role"] for slot in info.slots["texts"]]
    assert roles.count("avatar_name") == 4
    assert "content" not in roles, "content slots never render, so the default must not carry them"
    assert info.box_fit_default == "blur_fill"


def test_the_shipped_default_carries_its_fonts() -> None:
    """Without the two designer fonts, the default's own copy cannot be drawn anywhere."""
    info = package_service.read_package(package_service.default_package())
    families = {entry["family"] for entry in _manifest(info)["fonts"]}
    assert {"Baigo", "Alimama ShuHeiTi"} <= families


def test_the_default_shows_a_placeholder_not_a_real_person() -> None:
    """§19.37 ⑤: the four faces are one generic portrait, each ring keeping its own colour.

    Compared on the face, not on the file: the ring belongs to the slot (yellow, yellow, pink,
    blue), so the *bytes* legitimately differ. What must not differ is the photo inside.
    """
    info = package_service.read_package(package_service.default_package())
    lits = {name: data for name, data in info.files.items() if name.endswith("_lit.png")}
    assert len(lits) == 4
    faces = [_face(name, data) for name, data in sorted(lits.items())]
    # A tolerance, not equality: two slots are 249 px tall and two are 250, so the same photo
    # resamples a hair differently. 0 would test Pillow's downsampler; 12 still fails loudly
    # for any difference a viewer would call "a different person".
    for other in faces[1:]:
        assert abs(faces[0] - other).max() <= 12, "every slot must use the same placeholder portrait"
    assert len({_ring_colour(data) for data in lits.values()}) > 1, "the ring colour is per slot"


def _face(name: str, data: bytes) -> object:
    """The middle half of the slot: photo, not ring."""
    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = image.size
    centre = image.crop((w // 4, h // 4, w - w // 4, h - h // 4)).resize((8, 8), Image.LANCZOS)
    return np.asarray(centre).astype(int)


def _ring_colour(data: bytes) -> tuple[int, int, int]:
    """The most saturated opaque pixel — the ring's own colour."""
    import numpy as np
    from PIL import Image

    pixels = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA")).reshape(-1, 4)
    opaque = pixels[pixels[:, 3] == 255][:, :3].astype(int)
    spread = opaque.max(axis=1) - opaque.min(axis=1)
    return tuple(int(value) for value in opaque[spread.argmax()])


def _manifest(info: package_service.PackageInfo) -> dict[str, object]:
    """The manifest a reader validated, for the fields ``PackageInfo`` does not carry."""
    import json

    with zipfile.ZipFile(io.BytesIO(package_service.default_package())) as archive:
        data: dict[str, object] = json.loads(archive.read("manifest.json"))
    return data


def test_a_new_project_gets_the_default_template(
    client: TestClient, session: Session, settings: Settings
) -> None:
    project = project_service.create_project(session, settings, "第 1 期")
    session.commit()

    row = session.query(VideoTemplate).filter(VideoTemplate.project_id == project.id).one()
    assert row.status == "ready"
    assert row.box_fit_default == "blur_fill"
    assert Path(row.background_path).is_file()

    view = client.get(f"/api/projects/{project.id}/video/template").json()
    assert view["status"] == "ready"
    assert len(view["slots"]["avatars"]) == 4
    assert len(view["slots"]["texts"]) == 7
    # The poster is servable straight away — a template the UI cannot draw is not installed.
    assert client.get(view["slots"]["avatars"][0]["lit_url"]).status_code == 200


def test_a_project_can_be_created_without_a_template(session: Session, settings: Settings) -> None:
    """The escape hatch exists for callers that know there will be no video."""
    project = project_service.create_project(session, settings, "只有音频", with_template=False)
    session.commit()
    count = session.query(VideoTemplate).filter(VideoTemplate.project_id == project.id).count()
    assert count == 0


def test_a_broken_default_does_not_cost_the_project(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(package_service, "BUNDLED", tmp_path / "gone.template")
    project = project_service.create_project(session, settings, "照样建成")
    session.commit()
    assert session.get(type(project), project.id) is not None
    count = session.query(VideoTemplate).filter(VideoTemplate.project_id == project.id).count()
    assert count == 0


def test_a_missing_default_file_says_so() -> None:
    """A missing bundled file is a broken install, and the code has to say that."""
    original = package_service.BUNDLED
    package_service.BUNDLED = Path("definitely-not-here") / "default.template"
    try:
        with pytest.raises(AppError) as caught:
            package_service.default_package()
    finally:
        package_service.BUNDLED = original
    assert caught.value.code == "template_default_missing"


def test_the_default_can_be_installed_over_an_existing_template(
    client: TestClient, session: Session, settings: Settings
) -> None:
    """「用默认模板」 on a project that already has one replaces it, not appends a second row."""
    project = project_service.create_project(session, settings, "已有模板")
    session.commit()
    before = session.query(VideoTemplate).filter(VideoTemplate.project_id == project.id).count()
    assert before == 1

    response = client.post(f"/api/projects/{project.id}/video/template/default")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"

    rows = session.query(VideoTemplate).filter(VideoTemplate.project_id == project.id).all()
    assert len(rows) == 1
