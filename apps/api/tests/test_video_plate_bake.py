"""Baking the static plate, and the two preconditions it rests on (§19.30, E1).

Round 137 changed the inputs, not the arithmetic: the poster no longer contains the media-box
plate, so the bake is handed the **poster, the plate and the cropped stroke separately**
(`plate_bake.Art` carries each one's rect). Every measurement in this module still holds — a
plate baked from the three pieces has to be pixel-identical to one baked from the split poster.

The optimisation is only acceptable if it is **invisible**, and the only way to know that is to
render the same frame through both paths and compare. So the central test here is a real encode:
a small canvas, an avatar whose rect overlaps a piece of editable text, and a frame-by-frame
PSNR that must come out ``inf``.

The other tests pin the decisions that are easy to get wrong and silent when wrong:

* the text must be **left out** of the plate when it would be covered (inside the material box)
  or when the avatar under it is translucent — in both cases the export keeps the old graph
  rather than showing a wrong picture;
* a plate is named after a digest of its inputs, so editing the title produces a different file
  instead of a stale picture under an unchanged name (§19.11 trap 24);
* the patch is drawn on an avatar **already scaled by ffmpeg**, because that is what makes the
  graph's own ``scale`` an identity. Doing it in PIL on the unscaled PNG measured 54.38 dB.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
from PIL import Image

from animatter.media import plate_bake
from animatter.media.video import AvatarLayer, Layer, render_still

FFMPEG = shutil.which("ffmpeg")

CANVAS_W, CANVAS_H = 192, 108
#: The material box: drawn **above** the plate in the film, so baked text inside it vanishes.
BOX = (60, 20, 72, 68)
#: One avatar slot, with a native PNG larger than the slot so the graph has to scale it.
SLOT = (8, 8, 24, 24)
AVATAR_W, AVATAR_H = 40, 40


def _write(path: Path, image: Image.Image) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _solid(size: tuple[int, int], colour: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, colour)


def _text_png(path: Path, *, inside_box: bool = False) -> Path:
    """Opaque white blocks: one inside the avatar's rect, one clear of it.

    The one inside the avatar is what has to be carried into the avatar's own PNG; the one
    outside is what proves the rest of the text really did go into the plate.
    """
    image = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    image.paste((255, 255, 255, 255), (10, 10, 20, 20))
    if inside_box:
        image.paste((255, 255, 255, 255), (BOX[0] + 4, BOX[1] + 4, BOX[0] + 14, BOX[1] + 14))
    else:
        # Above the box, and outside the avatar rect.
        image.paste((255, 255, 255, 255), (100, 4, 110, 14))
    return _write(path, image)


def _fixture(tmp_path: Path, *, inside_box: bool = False, translucent: bool = False) -> dict:
    background = _write(tmp_path / "bg.png", _solid((CANVAS_W, CANVAS_H), (12, 40, 90, 255)))
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    # A stroke that crosses the box edge, so the two layers are not trivially separable.
    overlay.paste((250, 200, 0, 255), (BOX[0] - 2, BOX[1] - 2, BOX[0] + BOX[2] + 2, BOX[1]))
    stroke = _write(tmp_path / "overlay.png", overlay)
    avatar = Image.new("RGBA", (AVATAR_W, AVATAR_H), (30, 160, 90, 255))
    if translucent:
        # A hole right where the text patch lands: the old path would blend the plate through,
        # the patched path would blend the baked plate, and the two differ.
        avatar.paste((30, 160, 90, 128), (4, 4, 14, 14))
    lit = _write(tmp_path / "avatar_lit.png", avatar)
    dim = _write(tmp_path / "avatar_dim.png", _solid((AVATAR_W, AVATAR_H), (10, 60, 40, 255)))
    # The media-box plate, on its own, exactly as the compile now writes it: opaque, and sized
    # to the box rather than the canvas.
    plate_image = _solid((BOX[2], BOX[3]), (244, 244, 244, 255))
    plate = _write(tmp_path / "plate.png", plate_image)
    return {
        "base": background,
        "overlay": stroke,
        "plate": plate_bake.Art(plate, x=BOX[0], y=BOX[1], width=BOX[2], height=BOX[3]),
        "text": _text_png(tmp_path / "text.png", inside_box=inside_box),
        "lit": lit,
        "dim": dim,
        "slot": plate_bake.PatchSlot(
            name="avatar0", x=SLOT[0], y=SLOT[1], w=SLOT[2], h=SLOT[3], lit=lit, dim=dim
        ),
    }


def _bake(tmp_path: Path, fixture: dict, **kwargs: object) -> plate_bake.Plate:
    assert FFMPEG is not None
    return plate_bake.bake(
        str(FFMPEG),
        base=fixture["base"],
        plate=fixture["plate"],
        stroke=plate_bake.Art(fixture["overlay"]),
        text=fixture["text"],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        box_rect=kwargs.pop("box_rect", BOX),  # type: ignore[arg-type]
        slots=[fixture["slot"]],
        work_dir=tmp_path / "work",
    )


def _psnr(left: Path, right: Path) -> float:
    import numpy as np

    with Image.open(left) as one, Image.open(right) as two:
        assert one.size == two.size
        a = np.asarray(one.convert("RGB"), dtype=np.int32)
        b = np.asarray(two.convert("RGB"), dtype=np.int32)
    mse = float(np.mean((a - b) ** 2))
    return math.inf if mse == 0 else math.log10(255 * 255 / mse) * 10


# --- the preconditions --------------------------------------------------


def test_text_inside_the_material_box_is_not_baked(tmp_path: Path) -> None:
    """The box is drawn on top of the plate: baked text there would simply disappear."""
    fixture = _fixture(tmp_path, inside_box=True)
    plate = _bake(tmp_path, fixture)
    assert not plate.text_baked
    assert plate.skipped == "text_in_box"
    # The caller keeps the text layer, so the graph does not change at all.
    assert plate.text == fixture["text"]


def test_a_translucent_avatar_under_text_blocks_the_patch(tmp_path: Path) -> None:
    """Blending text over a half-transparent avatar is not the same as baking both."""
    fixture = _fixture(tmp_path, translucent=True)
    plate = _bake(tmp_path, fixture)
    assert not plate.text_baked
    assert plate.skipped.startswith("slot_translucent_")
    assert plate.avatar_sources == {}


def test_a_template_without_text_still_bakes_the_poster_and_stroke(tmp_path: Path) -> None:
    """The −16.8 % fallback: no text layer at all, and the plate is still worth making."""
    fixture = _fixture(tmp_path)
    assert FFMPEG is not None
    plate = plate_bake.bake(
        str(FFMPEG),
        base=fixture["base"],
        plate=fixture["plate"],
        stroke=plate_bake.Art(fixture["overlay"]),
        text=None,
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        box_rect=BOX,
        slots=[fixture["slot"]],
        work_dir=tmp_path / "work",
    )
    assert not plate.text_baked
    assert plate.text is None
    assert plate.avatar_sources == {}


# --- the digest ---------------------------------------------------------


def test_the_plate_name_follows_the_text(tmp_path: Path) -> None:
    """A new title must not reuse the previous plate (trap 24: "I changed it and nothing moved")."""
    fixture = _fixture(tmp_path)
    first = _bake(tmp_path, fixture)
    changed = _text_png(tmp_path / "text2.png")
    with Image.open(changed) as image:
        canvas = image.copy()
    canvas.paste((255, 255, 255, 255), (140, 60, 150, 70))
    canvas.save(fixture["text"])
    second = _bake(tmp_path, fixture)
    assert first.path != second.path
    assert not first.path.exists(), "the plate this export did not use must not linger"


def test_the_same_inputs_reuse_the_same_plate(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = _bake(tmp_path, fixture)
    second = _bake(tmp_path, fixture)
    assert first.path == second.path


def test_the_patch_is_drawn_on_an_ffmpeg_scaled_avatar(tmp_path: Path) -> None:
    """The graph scales the PNG to the slot rect; a patch drawn before that would be resampled."""
    fixture = _fixture(tmp_path)
    plate = _bake(tmp_path, fixture)
    assert plate.text_baked
    patched = plate.avatar_sources["avatar0"]
    for path in patched:
        with Image.open(path) as image:
            # Already at the slot's size, so the graph's own `scale` is an identity.
            assert image.size == (SLOT[2], SLOT[3])
    # And the text pixel really is in the patched avatar, not only in the plate.
    with Image.open(patched[0]) as image:
        red, _green, _blue, alpha = image.convert("RGBA").getpixel((12 - SLOT[0], 12 - SLOT[1]))
    assert (red, alpha) == (255, 255)


def test_a_slot_that_covers_no_text_is_left_alone(tmp_path: Path) -> None:
    """Patching a slot with no text in it is a wasted render, and a second source of drift."""
    fixture = _fixture(tmp_path)
    fixture["slot"] = plate_bake.PatchSlot(
        name="far", x=150, y=70, w=24, h=24, lit=fixture["lit"], dim=fixture["dim"]
    )
    plate = _bake(tmp_path, fixture)
    assert plate.text_baked, "text outside every avatar still goes into the plate"
    assert plate.avatar_sources == {}


# --- the picture actually is the same -----------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_the_baked_plate_renders_the_same_frame_as_the_old_graph(tmp_path: Path) -> None:
    """End to end, lossless on both sides: the acceptance bar is PSNR ``inf``, not ≥ 45 dB.

    The old graph overlays the poster, the stroke and the text above an avatar; the new one
    overlays a plate that already contains all three, with the text pixels inside the avatar's
    rect carried in the avatar's own PNG. Anything less than ``inf`` here means the bake
    reorders a blend, which is exactly the failure the 41.74 dB "text crushed under the avatar"
    version had.
    """
    assert FFMPEG is not None
    fixture = _fixture(tmp_path)
    plate = _bake(tmp_path, fixture)
    assert plate.text_baked and plate.patched_pixels > 0

    avatar = AvatarLayer(
        path=fixture["lit"], x=SLOT[0], y=SLOT[1], width=SLOT[2], height=SLOT[3], is_video=False
    )
    old = tmp_path / "old.png"
    render_still(
        str(FFMPEG),
        target=old,
        at_ms=0,
        width=CANVAS_W,
        height=CANVAS_H,
        background=fixture["base"],
        # The pre-split graph: the poster already had the plate baked in, then the stroke,
        # then the text. Now the plate is its own input, at its own rect, *before* the stroke.
        layers=[
            Layer(path=fixture["plate"].path, x=BOX[0], y=BOX[1], width=BOX[2], height=BOX[3]),
            Layer(path=fixture["overlay"], x=0, y=0),
        ],
        avatars=[avatar],
        text=Layer(path=fixture["text"], x=0, y=0),
    )

    patched_lit, _patched_dim = plate.avatar_sources["avatar0"]
    new = tmp_path / "new.png"
    render_still(
        str(FFMPEG),
        target=new,
        at_ms=0,
        width=CANVAS_W,
        height=CANVAS_H,
        background=plate.path,
        layers=[],
        avatars=[
            AvatarLayer(
                path=patched_lit, x=SLOT[0], y=SLOT[1], width=SLOT[2], height=SLOT[3], is_video=False
            )
        ],
        text=None,
    )
    assert _psnr(old, new) == math.inf


def test_without_the_patch_the_text_would_be_covered(tmp_path: Path) -> None:
    """The control: bake the text but skip the avatar patch, and the picture must differ.

    Without this, the PSNR test above would also pass on a version that never baked the text at
    all — it would be comparing the old graph with itself.
    """
    assert FFMPEG is not None
    fixture = _fixture(tmp_path)
    assert FFMPEG is not None
    plate = plate_bake.bake(
        str(FFMPEG),
        base=fixture["base"],
        plate=fixture["plate"],
        stroke=plate_bake.Art(fixture["overlay"]),
        text=fixture["text"],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        box_rect=BOX,
        # No slot is declared drawn, so no patch is made while the text still goes in.
        slots=[],
        work_dir=tmp_path / "work",
    )
    assert plate.text_baked
    old = tmp_path / "old.png"
    render_still(
        str(FFMPEG),
        target=old,
        at_ms=0,
        width=CANVAS_W,
        height=CANVAS_H,
        background=fixture["base"],
        layers=[
            Layer(path=fixture["plate"].path, x=BOX[0], y=BOX[1], width=BOX[2], height=BOX[3]),
            Layer(path=fixture["overlay"], x=0, y=0),
        ],
        avatars=[
            AvatarLayer(
                path=fixture["lit"],
                x=SLOT[0],
                y=SLOT[1],
                width=SLOT[2],
                height=SLOT[3],
                is_video=False,
            )
        ],
        text=Layer(path=fixture["text"], x=0, y=0),
    )
    new = tmp_path / "new.png"
    render_still(
        str(FFMPEG),
        target=new,
        at_ms=0,
        width=CANVAS_W,
        height=CANVAS_H,
        background=plate.path,
        layers=[],
        avatars=[
            AvatarLayer(
                path=fixture["lit"],
                x=SLOT[0],
                y=SLOT[1],
                width=SLOT[2],
                height=SLOT[3],
                is_video=False,
            )
        ],
        text=None,
    )
    assert _psnr(old, new) < math.inf


# --- wiring into the export ---------------------------------------------


def _template(tmp_path: Path, fixture: dict) -> object:
    """A stand-in template row with the two compiled PNGs this module reads."""
    from animatter.models import VideoTemplate

    bare = fixture["base"].with_name("background_bare.png")
    shutil.copy(fixture["base"], bare)
    return VideoTemplate(
        id="vtpl_test",
        project_id="prj_test",
        background_path=str(fixture["base"]),
        overlay_path=str(fixture["overlay"]),
    )


def _track(fixture: dict) -> object:
    from animatter.core.video_avatar import Interval
    from animatter.services.video_render import AvatarTrack

    return AvatarTrack(
        name="avatar0",
        x=SLOT[0],
        y=SLOT[1],
        width=SLOT[2],
        height=SLOT[3],
        lit_path=fixture["lit"],
        dim_path=fixture["dim"],
        lit=[Interval(0, 1000)],
    )


def test_the_switch_off_returns_no_plate(tmp_path: Path, settings: object) -> None:
    """`video_plate_bake = false` must leave the render path untouched, not "less baked"."""
    from animatter.services.video_render import _plate_for

    fixture = _fixture(tmp_path)
    settings.video_plate_bake = False  # type: ignore[attr-defined]
    plate = _plate_for(
        settings,  # type: ignore[arg-type]
        _template(tmp_path, fixture),  # type: ignore[arg-type]
        text_png=fixture["text"],
        tracks=[_track(fixture)],  # type: ignore[list-item]
        work_dir=tmp_path / "work",
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        box_rect={"x": BOX[0], "y": BOX[1], "w": BOX[2], "h": BOX[3]},
    )
    assert plate is None


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_the_export_plate_reports_the_slots_it_patched(
    tmp_path: Path, settings: object
) -> None:
    """The map the export hands to `_avatar_layers` names slots, not files."""
    from animatter.services.video_render import _plate_for

    fixture = _fixture(tmp_path)
    settings.video_plate_bake = True  # type: ignore[attr-defined]
    plate = _plate_for(
        settings,  # type: ignore[arg-type]
        _template(tmp_path, fixture),  # type: ignore[arg-type]
        text_png=fixture["text"],
        tracks=[_track(fixture)],  # type: ignore[list-item]
        work_dir=tmp_path / "work",
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        box_rect={"x": BOX[0], "y": BOX[1], "w": BOX[2], "h": BOX[3]},
    )
    assert plate is not None
    assert plate.text_baked
    assert plate.text is None, "the export must stop overlaying the text layer it baked"
    assert set(plate.avatar_sources) == {"avatar0"}


def test_a_patched_slot_reaches_the_track_as_its_own_stills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_avatar_layers` must substitute the patched pair, or the name vanishes under the avatar.

    Captured rather than encoded: the point is which files the concat list is built from, and a
    real qtrle encode would only add seconds to the suite without testing anything more.
    """
    from animatter.media import avatar_track
    from animatter.services import video_render

    fixture = _fixture(tmp_path)
    seen: list[object] = []

    def capture(_ffmpeg: str, *, target: Path, pieces: list[object], work_dir: Path) -> Path:
        seen.extend(pieces)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return target

    monkeypatch.setattr(avatar_track, "build_track", capture)
    patched_lit = tmp_path / "patched_lit.png"
    patched_dim = tmp_path / "patched_dim.png"
    shutil.copy(fixture["lit"], patched_lit)
    shutil.copy(fixture["dim"], patched_dim)

    class _Settings:
        ffmpeg_path = "ffmpeg"

    video_render._avatar_layers(
        _Settings(),  # type: ignore[arg-type]
        [_track(fixture)],  # type: ignore[list-item]
        duration_ms=1000,
        work_dir=tmp_path / "work",
        sources={"avatar0": (patched_lit, patched_dim)},
    )
    paths = {piece.path for piece in seen}  # type: ignore[attr-defined]
    assert paths == {patched_lit}
    assert fixture["lit"] not in paths

