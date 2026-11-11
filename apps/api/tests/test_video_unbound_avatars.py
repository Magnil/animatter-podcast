"""Tests for the unbound-avatar fix: the slots with no track still need their photo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from tests.test_corrections import seed_version

from animatter.core.video_avatar import Interval
from animatter.media.video import AvatarLayer


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    from fastapi.testclient import TestClient

    from animatter.config import Settings
    from animatter.main import create_app

    data_dir = tmp_path_factory.mktemp("unbound") / "data"
    app = create_app(Settings(data_dir=data_dir, host="127.0.0.1", log_level="ERROR"))
    with TestClient(app) as test_client:
        yield test_client


def _template(client, project_id: str) -> None:
    """A minimal ready template row: no compile, just the paths this test reads."""
    db = client.app.state.db  # type: ignore[attr-defined]
    build = Path(client.app.state.settings.data_dir) / "build"
    build.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        for suffix in ("lit", "dim"):
            path = build / f"avatar{index}_{suffix}.png"
            if not path.is_file():
                Image.new("RGBA", (236, 249), (200, 30 + index * 40, 90, 255)).save(path)

    from animatter.ids import new_id, utcnow_iso
    from animatter.models import VideoTemplate

    with db.session() as session:
        session.add(
            VideoTemplate(
                id=new_id("vtpl"),
                project_id=project_id,
                source_path=str(build / "source.psd"),
                source_sha256="0" * 64,
                canvas_w=1920,
                canvas_h=1080,
                background_path=str(build / "background.png"),
                overlay_path="",
                box_rect='{"x": 240, "y": 209, "w": 1440, "h": 810}',
                box_fit_default="blur_fill",
                # ``json.dumps``, not string concatenation: this runs on Windows, where a
                # path holds backslashes and ``\a`` is not a legal JSON escape. The service
                # degrades a malformed blob to "no slots" rather than raising, so the first
                # version of this test quietly tested nothing.
                slots_json=json.dumps(
                    {
                        "avatars": [
                            {
                                "name": f"头像{index}",
                                "rect": {"x": 40, "y": 200 + index * 200, "w": 177, "h": 188},
                                "lit_path": str(build / f"avatar{index}_lit.png"),
                                "dim_path": str(build / f"avatar{index}_dim.png"),
                            }
                            for index in range(4)
                        ]
                    }
                ),
                status="ready",
                error="",
                created_at=utcnow_iso(),
            )
        )
        session.commit()


def test_unbound_slots_get_a_static_photo_and_bound_ones_do_not(client) -> None:
    """The bug this fixes: binding two speakers made the other two faces disappear.

    ``_base_background`` switches to ``background_bare.png`` (every avatar stripped) as soon as
    *any* slot has a track, but tracks only exist for bound slots. Measured on the sample's own
    still: two faces, two empty circles.
    """
    from animatter.services import video_render, video_templates

    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = seed_version(session, text="测试")
        project_id = version.project_id
        session.commit()
    _template(client, project_id)

    with db.session() as session:
        template = video_templates.require(session, project_id)
        tracks = [
            video_render.AvatarTrack(
                name="头像2",
                x=40,
                y=600,
                width=236,
                height=249,
                lit_path=Path(template.background_path).parent / "avatar2_lit.png",
                dim_path=Path(template.background_path).parent / "avatar2_dim.png",
                lit=[Interval(start_ms=0, end_ms=1000)],
            )
        ]
        statics = video_render.static_avatar_layers(session, template, tracks)

    names = {Path(layer.path).name.split("_")[0] for layer in statics}
    assert names == {"avatar0", "avatar1", "avatar3"}, names
    assert all(isinstance(layer, AvatarLayer) and not layer.is_video for layer in statics)


def test_no_bound_slot_means_no_static_layers(client) -> None:
    """With nothing bound the poster already has all four photos, so adding them double-draws."""
    from animatter.services import video_render, video_templates

    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as session:
        version = seed_version(session, text="另一个测试")
        project_id = version.project_id
        session.commit()
    _template(client, project_id)

    with db.session() as session:
        template = video_templates.require(session, project_id)
        assert video_render.static_avatar_layers(session, template, []) == []
