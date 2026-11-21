"""What the renderer reads out of a compiled template, and what it reads when it can't (§19.35).

The compile splits the poster into three pieces, and the render side has exactly one way to
learn where they go: the ``art`` block in ``slots_json``. So the interesting cases are not the
happy path — they are the templates that **predate** the split, which must keep rendering the
way they always did rather than falling back to "no plate, no stroke" and painting a bare poster
behind the box content.

``poster_of`` and ``box_source`` are separate functions instead of two ``if`` statements inside
the render loop because each one has a wrong answer that looks fine: a poster that still holds
the plate makes a moved plate double-draw, and a gap excerpt taken from the poster instead of
the plate drifts by the plate's feathering (2 px across, 3 px down) the first time the layout
moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.ids import new_id, utcnow_iso
from animatter.models import VideoTemplate
from animatter.services import video_templates

CANVAS = (1920, 1080)
BOX = {"x": 240, "y": 209, "w": 1440, "h": 810}
PLATE_RECT = {"x": 238, "y": 206, "w": 1444, "h": 815}
STROKE_RECT = {"x": 231, "y": 198, "w": 1456, "h": 829}


@pytest.fixture()
def project_id(session: Session, client: TestClient) -> str:
    """A project to hang templates on.

    The rows here are never committed — every test inspects them in the same session — but the
    foreign key still needs a real project id, and a made-up string would fail only on flush.
    """
    version = seed_version(session, text="模板产物")
    session.commit()
    return version.project_id


def write_png(path: Path, size: tuple[int, int] = CANVAS, colour=(20, 20, 20, 255)) -> Path:
    Image.new("RGBA", size, colour).save(path)
    return path


def make_template(
    session: Session,
    project_id: str,
    build: Path,
    *,
    art: dict[str, object] | None,
    with_open: bool = True,
) -> VideoTemplate:
    """A row whose build directory holds the files the shape under test needs."""
    build.mkdir(parents=True, exist_ok=True)
    write_png(build / "background.png")
    write_png(build / "background_bare.png")
    write_png(build / "overlay.png")
    if with_open:
        write_png(build / "background_open.png")
        write_png(build / "background_bare_open.png")
    if art is not None:
        write_png(build / "plate.png", (1444, 815), (244, 244, 244, 255))
    row = VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path="",
        source_sha256="0" * 64,
        canvas_w=CANVAS[0],
        canvas_h=CANVAS[1],
        background_path=str(build / "background.png"),
        overlay_path=str(build / "overlay.png"),
        box_rect=json.dumps(BOX),
        box_fit_default="blur_fill",
        slots_json=json.dumps({"avatars": [], "texts": [], "notes": [], "art": art}),
        bindings_json="{}",
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def split_art() -> dict[str, object]:
    return {
        "plate": {"path": "plate.png", "rect": PLATE_RECT},
        "stroke": {"path": "overlay.png", "rect": STROKE_RECT},
    }


def test_a_split_template_reports_both_artworks_with_their_rects(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build", art=split_art())
    plate, stroke = video_templates.art_of(row)
    assert plate is not None and stroke is not None
    assert plate.path == tmp_path / "build" / "plate.png"
    assert plate.as_rect() == PLATE_RECT
    assert stroke.as_rect() == STROKE_RECT
    # The signature is what the cache keys read; it must move with the rect, not just the file.
    assert "plate.png@" in video_templates.art_signature(row)
    moved = dict(split_art())
    moved["plate"] = {"path": "plate.png", "rect": {**PLATE_RECT, "x": PLATE_RECT["x"] + 40}}
    other = make_template(session, project_id, tmp_path / "moved", art=moved)
    assert video_templates.art_signature(other) != video_templates.art_signature(row)


def test_a_pre_split_template_reports_a_full_canvas_stroke_and_no_plate(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The rows that exist today have no ``art`` block. They must keep the old graph.

    Reporting nothing would drop the frame stroke entirely — a bare poster behind the box
    content, which reads as "the template lost its frame" and is the worst way to ship a
    layout refactor.
    """
    row = make_template(session, project_id, tmp_path / "legacy", art=None, with_open=False)
    plate, stroke = video_templates.art_of(row)
    assert plate is None
    assert stroke is not None
    assert stroke.path == tmp_path / "legacy" / "overlay.png"
    assert stroke.as_rect() == {"x": 0, "y": 0, "w": CANVAS[0], "h": CANVAS[1]}
    assert video_templates.art_signature(row) == "-|overlay.png@0,0,1920x1080"


def test_a_broken_art_block_falls_back_instead_of_raising(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """``slots_json`` is user-visible JSON in a SQLite column; it can be anything.

    The rule the whole file follows: malformed means *older*, never *crash*. A render that
    raises on a bad rect cannot even show the user their template to fix it.
    """
    row = make_template(
        session,
        project_id,
        tmp_path / "broken",
        art={"plate": {"path": "plate.png"}, "stroke": "nope"},
    )
    plate, stroke = video_templates.art_of(row)
    assert plate is None
    assert stroke is not None and stroke.as_rect() == {"x": 0, "y": 0, "w": 1920, "h": 1080}


def test_the_art_name_cannot_walk_out_of_the_build_directory(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The name comes from a JSON column, so it is input, not a fact."""
    escaped = {
        "plate": {"path": "../../../etc/plate.png", "rect": PLATE_RECT},
        "stroke": {"path": "overlay.png", "rect": STROKE_RECT},
    }
    row = make_template(session, project_id, tmp_path / "escape", art=escaped)
    plate, _stroke = video_templates.art_of(row)
    assert plate is not None
    assert plate.path.parent == tmp_path / "escape"


def test_the_poster_prefers_the_open_twin_and_falls_back_to_the_flat_one(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    split = make_template(session, project_id, tmp_path / "open", art=split_art())
    assert video_templates.poster_of(split, bare=False).name == "background_open.png"
    assert video_templates.poster_of(split, bare=True).name == "background_bare_open.png"

    legacy = make_template(session, project_id, tmp_path / "flat", art=None, with_open=False)
    assert video_templates.poster_of(legacy, bare=False).name == "background.png"
    assert video_templates.poster_of(legacy, bare=True).name == "background_bare.png"


def test_a_gap_is_cut_from_the_plate_when_the_box_fits_inside_it(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "gap", art=split_art())
    source, origin = video_templates.box_source(row, BOX)
    assert source.name == "plate.png"
    assert origin == (PLATE_RECT["x"], PLATE_RECT["y"])


def test_a_gap_falls_back_to_the_poster_when_the_box_sticks_out(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """A layout edit can push the box past the plate; the poster is then the only image that
    covers it. Cropping there instead would show transparent pixels for the overhang."""
    row = make_template(session, project_id, tmp_path / "overhang", art=split_art())
    wide = {"x": 100, "y": BOX["y"], "w": BOX["w"], "h": BOX["h"]}
    source, origin = video_templates.box_source(row, wide)
    assert source.name == "background.png"
    assert origin == (0, 0)


def test_a_missing_plate_file_falls_back_to_the_poster(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The row says there is a plate; the disk disagrees. The disk wins — an ffmpeg input
    that does not exist fails the whole export with a message about a PNG nobody named."""
    build = tmp_path / "gone"
    row = make_template(session, project_id, build, art=split_art())
    (build / "plate.png").unlink()
    source, origin = video_templates.box_source(row, BOX)
    assert source.name == "background.png"
    assert origin == (0, 0)
