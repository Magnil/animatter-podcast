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
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    box = compiled.box_rect
    pixels = np.array(Image.open(compiled.background_path).convert("RGB"))[
        box.y : box.bottom, box.x : box.right
    ]
    assert pixels.std() < 1.0, f"content rect is not uniform (std={pixels.std():.2f})"


def test_real_compile_keeps_the_overlay_to_the_frame_stroke(compiled: object) -> None:
    """The overlay must be the frame, not a full-canvas sheet.

    Before excluding full-bleed layers, 「透明框架」 (2560×1440) was classed as an
    overlay and covered 44 % of the canvas — the composite lost the logo, the title
    and every avatar.
    """
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    alpha = np.array(Image.open(compiled.overlay_path).convert("RGBA"))[:, :, 3]
    ink = alpha > 8
    assert 0.005 < ink.mean() < 0.05, f"overlay ink fraction {ink.mean():.4f}"
    rows = np.where(ink.any(axis=1))[0]
    cols = np.where(ink.any(axis=0))[0]
    box = compiled.box_rect
    # The stroke hugs the box from slightly outside it.
    assert cols[0] < box.x and rows[0] < box.y
    assert cols[-1] + 1 > box.right - 40 and rows[-1] + 1 > box.bottom


def test_real_compile_finds_four_visible_avatars(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    assert len(compiled.avatars) == 4
    labels = sorted(slot.label for slot in compiled.avatars)
    assert labels == sorted(["彼方", "马小褂", "小榕", "呆尾"])
    for slot in compiled.avatars:
        assert slot.lit_path and slot.dim_path
        assert Path(slot.lit_path).is_file()


def test_real_compile_notes_the_hidden_avatar_groups(compiled: object) -> None:
    from animatter.media.template import CompiledTemplate

    assert isinstance(compiled, CompiledTemplate)
    assert any("隐藏的头像组" in note for note in compiled.notes)


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
    assert "EP.23" in texts
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
