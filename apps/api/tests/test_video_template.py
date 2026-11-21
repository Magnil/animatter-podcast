"""PSD template compilation: geometry, classification, and one real compile.

The real-PSD test is deliberately in here rather than in a separate slow suite:
this module's whole output is *pixels*, and every value worth pinning (the content
rect, the avatar slots, the overlay's contents) can only be established against the
actual file. A compile takes ~6 s, so there is exactly one of them; everything else
runs on synthetic rects.

What was measured on ``samples/播客模板.psd`` (2560×1440, 58 layers) and is pinned
below:

* the media group is ``(320, 278)-(2240, 1358)``, i.e. exactly 1920×1080 = 16:9;
* its plate is a single full-bleed ``pixel`` layer; the poster art and two text
  layers inside the group are *content* and must be removed;
* four avatar groups are visible (two per side) and four more are hidden;
* ``内框描边`` (the blue frame) overlaps the content rect, so it must be an overlay;
* ``透明框架`` is full-bleed and must **not** be one — treating it as an overlay
  painted a near-opaque white sheet over the whole poster.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from animatter.errors import AppError
from animatter.media.template import (
    Rect,
    bbox_to_rect,
    even_rect,
    scale_rect,
)

#: ``samples/`` lives at the repo root, two levels above ``apps/api``.
REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_PSD = REPO_ROOT / "samples" / "播客模板.psd"


# --- geometry -----------------------------------------------------------


def test_bbox_to_rect_rejects_degenerate_boxes() -> None:
    # An empty (0,0,0,0) group is what a switched-off PSD group reports; treating
    # it as a slot would draw a zero-size avatar.
    assert bbox_to_rect((0, 0, 0, 0)) is None
    assert bbox_to_rect((10, 10, 5, 20)) is None
    assert bbox_to_rect(None) is None


def test_bbox_to_rect_reads_the_half_open_box() -> None:
    assert bbox_to_rect((320, 278, 2240, 1358)) == Rect(x=320, y=278, w=1920, h=1080)


def test_scale_rect_keeps_the_edges_aligned() -> None:
    """``x + w`` must equal the scaled right edge, or a sliver of poster shows."""
    rect = Rect(x=320, y=278, w=1920, h=1080)
    scaled = scale_rect(rect, 0.75)
    assert scaled == Rect(x=240, y=209, w=1440, h=810)
    assert scaled.right == int(rect.right * 0.75 + 0.5)


def test_scale_rect_of_an_odd_box_still_aligns() -> None:
    rect = Rect(x=56, y=267, w=236, h=250)
    scaled = scale_rect(rect, 0.75)
    assert scaled.right == int((56 + 236) * 0.75 + 0.5)
    assert scaled.bottom == int((267 + 250) * 0.75 + 0.5)


def test_even_rect_only_grows() -> None:
    assert even_rect(Rect(x=3, y=5, w=1441, h=811)) == Rect(x=3, y=5, w=1442, h=812)


def test_even_rect_leaves_even_boxes_alone() -> None:
    rect = Rect(x=240, y=209, w=1440, h=810)
    assert even_rect(rect) == rect


def test_rect_intersects_is_symmetric_and_excludes_touching() -> None:
    left = Rect(x=0, y=0, w=10, h=10)
    right = Rect(x=10, y=0, w=10, h=10)
    assert not left.intersects(right)  # half-open: sharing an edge is not overlap
    assert Rect(x=5, y=5, w=10, h=10).intersects(left)
    assert left.intersects(Rect(x=5, y=5, w=10, h=10))


# --- real compile -------------------------------------------------------


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> object:
    from animatter.media.template import compile_template

    if not SAMPLE_PSD.is_file():
        pytest.skip("samples/播客模板.psd 不在仓库里")
    out = tmp_path_factory.mktemp("template")
    return compile_template(SAMPLE_PSD, out)


def test_real_compile_finds_the_media_box(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    # 1920×1080 content area scaled by 0.75 from (320,278)-(2240,1358).
    assert compiled.box_rect == Rect(x=240, y=209, w=1440, h=810)
    assert compiled.canvas_w == 1920 and compiled.canvas_h == 1080


def test_real_compile_emits_both_backgrounds(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    background = Image.open(compiled.background_path)
    bare = Image.open(compiled.background_bare_path)
    assert background.size == (1920, 1080)
    assert bare.size == (1920, 1080)
    # P1 flattens avatars in; P3 starts from the bare one. They must differ, or
    # the bare version silently lost its avatars.
    assert not np.array_equal(np.array(background), np.array(bare))


def test_real_compile_leaves_the_content_rect_empty(compiled: object) -> None:
    """The box has to be a clean plate, or dropped content is unreadable.

    Measured before the fix: 12 distinct colours and std 2.93 inside the box,
    because the PSD's own poster and two text layers were still in the background.

    Since round 137 the flat plate lives in ``plate.png``; ``background_open.png`` has the box
    rect **transparent** there, which is the same promise seen from the other side — nothing the
    PSD drew may remain under the box.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    box = compiled.box_rect
    pixels = np.array(Image.open(compiled.background_path).convert("RGB"))[
        box.y : box.bottom, box.x : box.right
    ]
    assert pixels.std() < 1.0, f"content rect is not uniform (std={pixels.std():.2f})"

    # What the split promises is narrower than "transparent under the box": the frame stroke
    # legitimately crosses into it (5930 semi-opaque pixels on the sample), and the stroke is
    # drawn on top of the plate anyway. The promise is that **the plate** was the only thing
    # removed — so nothing outside the plate rect may have changed.
    flat = np.array(Image.open(compiled.background_bare_path).convert("RGBA")).astype(np.int32)
    opened = np.array(Image.open(compiled.background_bare_open_path).convert("RGBA")).astype(
        np.int32
    )
    changed = np.abs(flat - opened).max(axis=2) > 0
    rect = compiled.plate_rect
    assert rect is not None
    outside = np.ones(changed.shape, dtype=bool)
    outside[rect.y : rect.bottom, rect.x : rect.right] = False
    assert not (changed & outside).any(), "the split changed pixels outside the plate rect"
    assert changed.any(), "the split removed nothing — the plate is still in the poster"


def test_real_compile_keeps_the_overlay_to_the_frame_stroke(compiled: object) -> None:
    """The overlay must be the frame, not a full-canvas sheet.

    Before excluding full-bleed layers, 「透明框架」 (2560×1440) was classed as an
    overlay and covered 44 % of the canvas — the composite lost the logo, the title
    and every avatar.

    Since round 137 the file is **cropped** to the stroke's own bounds and its rect is
    recorded, so the ink now fills the PNG instead of sitting inside a 1920×1080 sheet. The
    thing worth pinning moved with it: the rect still hugs the box from slightly outside.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    alpha = np.array(Image.open(compiled.overlay_path).convert("RGBA"))[:, :, 3]
    # "Ink" for the fraction stays at alpha > 8 — the stroke is a soft blue glow and counting
    # every 1/255 pixel would call the anti-aliasing halo part of the frame.
    ink = alpha > 8
    assert 0.005 < ink.mean() < 0.10, f"overlay ink fraction {ink.mean():.4f}"
    # The crop is tight by *any* ink, which is the bound the split actually used.
    rows = np.where((alpha > 0).any(axis=1))[0]
    cols = np.where((alpha > 0).any(axis=0))[0]
    assert (int(cols[0]), int(rows[0])) == (0, 0)
    assert (int(cols[-1]) + 1, int(rows[-1]) + 1) == alpha.shape[::-1]
    rect = compiled.overlay_rect
    assert rect is not None
    assert (rect.w, rect.h) == (alpha.shape[1], alpha.shape[0])
    box = compiled.box_rect
    # And placed at its rect it hugs the box from slightly outside it.
    assert rect.x < box.x and rect.y < box.y
    assert rect.right > box.right - 40 and rect.bottom > box.bottom


def test_real_compile_splits_the_plate_out_of_the_poster(compiled: object) -> None:
    """The white media-box plate is its own file now, at its own rect (§19.35).

    Recombining the pieces has to reproduce a pre-split compile **exactly** — that is the whole
    acceptance bar for this refactor, and it is checked against the flat twins that are still
    written, so the test does not need a stored golden image.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    assert compiled.plate_path is not None and compiled.plate_rect is not None
    assert compiled.plate_path.is_file()
    plate = Image.open(compiled.plate_path).convert("RGBA")
    rect = compiled.plate_rect
    assert (plate.width, plate.height) == (rect.w, rect.h)
    # Opaque by construction: the split is accepted only if a fully opaque patch at this rect
    # reproduces the poster, and a translucent patch is what broke the first three attempts.
    assert np.asarray(plate.getchannel("A")).min() == 255
    # Inside the box, so the box content has a clean plate to draw on.
    assert rect.x <= compiled.box_rect.x and rect.y <= compiled.box_rect.y
    assert rect.right >= compiled.box_rect.right

    stroke_rect = compiled.overlay_rect
    assert stroke_rect is not None
    stroke = Image.new("RGBA", (compiled.canvas_w, compiled.canvas_h), (0, 0, 0, 0))
    with Image.open(compiled.overlay_path) as image:
        stroke.alpha_composite(image.convert("RGBA"), dest=(stroke_rect.x, stroke_rect.y))

    for flat, opened in (
        (compiled.background_path, compiled.background_open_path),
        (compiled.background_bare_path, compiled.background_bare_open_path),
    ):
        assert opened is not None and opened.is_file()
        expected = Image.open(flat).convert("RGBA")
        expected.alpha_composite(stroke)
        rebuilt = Image.open(opened).convert("RGBA")
        rebuilt.alpha_composite(plate, dest=(rect.x, rect.y))
        rebuilt.alpha_composite(stroke)
        diff = np.abs(
            np.asarray(rebuilt).astype(np.int32) - np.asarray(expected).astype(np.int32)
        )
        assert int(diff.max()) == 0, f"{Path(flat).name}: the split is not pixel-identical"


def test_real_compile_records_the_art_rects_in_slots_json(compiled: object) -> None:
    """The rects have to survive the JSON round trip, or the render side cannot place anything."""
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    art = compiled.slots_json()["art"]
    assert art["plate"]["path"] == Path(compiled.plate_path or "").name
    assert art["plate"]["rect"] == {
        "x": compiled.plate_rect.x if compiled.plate_rect else -1,
        "y": compiled.plate_rect.y if compiled.plate_rect else -1,
        "w": compiled.plate_rect.w if compiled.plate_rect else -1,
        "h": compiled.plate_rect.h if compiled.plate_rect else -1,
    }
    assert art["stroke"]["path"] == Path(compiled.overlay_path).name
    assert art["stroke"]["rect"]["x"] == (compiled.overlay_rect.x if compiled.overlay_rect else -1)
    # File names only: the build directory is derived from ``background_path``, so an absolute
    # path here would break the moment the data directory moved.
    assert "/" not in art["plate"]["path"] and "\\" not in art["plate"]["path"]
    assert "/" not in art["stroke"]["path"] and "\\" not in art["stroke"]["path"]


def test_real_compile_finds_four_visible_avatars(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    assert len(compiled.avatars) == 4
    labels = sorted(slot.label for slot in compiled.avatars)
    assert labels == sorted(["彼方", "马小褂", "小榕", "呆尾"])
    for slot in compiled.avatars:
        assert slot.lit_path and slot.dim_path
        assert Path(slot.lit_path).is_file()


def test_real_compile_keeps_notes_to_actionable_problems(compiled: object) -> None:
    """``notes`` is the panel's only prose, and it must earn its space (round 99).

    It used to carry 「PSD 里有 4 个隐藏的头像组，未计入版式」 and 「素材框尺寸已取偶到 1440×810」 —
    both true, both unactionable, and together they pushed the settings off the column. The
    sample PSD is well-formed, so a compile of it produces **no notes at all**.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    assert compiled.notes == []


def test_real_compile_dim_changes_only_the_photo(compiled: object) -> None:
    """「暗」 must not touch the plate or the alpha — both earlier tries failed there.

    Dimming the whole group turned the white plate grey; lowering the group's alpha
    let the poster show through. Both produced a visible box. This pins the property
    that actually fixes it: alpha identical, and every changed pixel inside the
    photo's own rect.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    for slot in compiled.avatars:
        lit = np.array(Image.open(slot.lit_path).convert("RGBA")).astype(int)
        dim = np.array(Image.open(slot.dim_path).convert("RGBA")).astype(int)
        assert np.array_equal(lit[:, :, 3], dim[:, :, 3]), f"{slot.name}: alpha changed"

        changed = np.abs(lit[:, :, :3] - dim[:, :, :3]).sum(axis=2) > 0
        assert changed.any(), f"{slot.name}: dim is identical to lit"
        photo = slot.photo
        right = min(photo.right, changed.shape[1])
        bottom = min(photo.bottom, changed.shape[0])
        inside = np.zeros(changed.shape, dtype=bool)
        inside[photo.y : bottom, photo.x : right] = True
        assert not (changed & ~inside).any(), f"{slot.name}: changed pixels outside the photo"


def test_real_compile_dim_leaves_the_plate_alone(compiled: object) -> None:
    """The grey square: the photo's *rect* covers the group's own white plate.

    Dimming the rect turns that plate grey, so a dimmed avatar comes out with a grey box
    behind its circle. Measured on the sample: the avatar's top-left plate corner was
    ``(129,128,128)`` before the mask and is ``(252,247,244)`` after.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    for slot in compiled.avatars:
        lit = np.array(Image.open(slot.lit_path).convert("RGBA")).astype(int)
        dim = np.array(Image.open(slot.dim_path).convert("RGBA")).astype(int)
        # The plate corners are outside the photo's *visible* pixels but inside its rect.
        photo = slot.photo
        corners = [
            (photo.y + 2, photo.x + 2),
            (photo.y + 2, min(photo.right, lit.shape[1]) - 3),
        ]
        for y, x in corners:
            before = lit[y, x, :3]
            after = dim[y, x, :3]
            # "Unchanged" is what proves the plate was skipped — but only where the plate
            # actually shows at that corner, which is where lit and the photo differ.
            if not np.array_equal(before, after):
                continue
            assert np.array_equal(before, after), f"{slot.name}: plate corner changed at {y},{x}"


def test_real_compile_dim_has_no_white_speckle(compiled: object) -> None:
    """Masking on colour alone leaves the photo's white line art undimmed.

    The plate composite is pure white RGB under a transparent alpha, so "colour changed"
    is false exactly where the photo draws a white outline — measured: the whole character
    came out stippled with white when only colour was compared.

    The assertion has to be phrased over the *untouched* pixels rather than over
    "bright pixels": the circle's corners inside the photo's rect are plate, they stay
    identical to the lit PNG on purpose, and they are bright. What must never happen is a
    pixel that was dimmed and still reads as white.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    for slot in compiled.avatars:
        lit = np.array(Image.open(slot.lit_path).convert("RGBA")).astype(int)
        dim = np.array(Image.open(slot.dim_path).convert("RGBA")).astype(int)
        photo = slot.photo
        right = min(photo.right, dim.shape[1])
        bottom = min(photo.bottom, dim.shape[0])
        region_lit = lit[photo.y : bottom, photo.x : right]
        region_dim = dim[photo.y : bottom, photo.x : right]

        untouched = np.all(region_lit == region_dim, axis=2)
        bright = (region_dim[:, :, 3] > 0) & (region_dim[:, :, :3].max(axis=2) > 200)
        speckle = bright & ~untouched
        assert not speckle.any(), (
            f"{slot.name}: {int(speckle.sum())} pixels were dimmed yet stayed bright "
            "— that is the white line art being skipped"
        )


def test_real_compile_reads_text_slots_without_rewriting_them(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    texts = {slot.text for slot in compiled.texts}
    # ``**`` is the round-98 colour markup: the PSD draws ``EP.`` blue and ``23`` red, so the
    # default value carries that split instead of losing it (§19.13.12).
    assert "EP.**23**" in texts
    # Newlines are normalised so the UI can show them; psd-tools returns ``\r``.
    assert any("\n" in text or len(text) > 10 for text in texts)


# --- failures -----------------------------------------------------------


def test_missing_file_is_a_404_with_a_hint(tmp_path: Path) -> None:
    from animatter.media.template import compile_template

    with pytest.raises(AppError) as excinfo:
        compile_template(tmp_path / "nope.psd", tmp_path / "out")
    assert excinfo.value.status == 404
    assert excinfo.value.hint


def test_a_non_psd_file_fails_with_the_parser_error(tmp_path: Path) -> None:
    from animatter.media.template import compile_template

    bogus = tmp_path / "not.psd"
    bogus.write_bytes(b"this is not a photoshop file")
    with pytest.raises(AppError) as excinfo:
        compile_template(bogus, tmp_path / "out")
    assert excinfo.value.code == "template_unreadable"
    assert "psd-tools" in excinfo.value.hint
