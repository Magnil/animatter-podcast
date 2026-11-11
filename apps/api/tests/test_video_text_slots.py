"""Editable text slots and replaceable avatar photos (§19.12).

The classification tests run on synthetic candidates — the rules ("inside an avatar group is
a name", "a header-band layer matching ``EP.23`` is the episode number") are pure logic and
belong in a fast test. The one real compile pins what the sample PSD actually produces: nine
text slots, four avatar frames with a mask, and the two faces the design calls for.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from psd_tools import PSDImage

from animatter.core import textslot
from animatter.errors import AppError
from animatter.media import fonts
from animatter.media import text_overlay as overlay
from animatter.media.template import Rect, compile_template

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_PSD = REPO_ROOT / "samples" / "播客模板.psd"

CANVAS_W = 2560
CANVAS_H = 1440


# --- classification -----------------------------------------------------


def _candidate(
    name: str,
    text: str,
    *,
    x: int,
    y: int,
    w: int = 200,
    h: int = 60,
    size: float = 60.0,
    avatar: str | None = None,
) -> textslot.Candidate:
    return textslot.Candidate(
        name=name,
        text=text,
        box=textslot.Box(x=x, y=y, w=w, h=h),
        font_size=size,
        colour="#000000",
        avatar=avatar,
    )


def test_episode_number_is_recognised_in_the_spellings_that_exist() -> None:
    for text in ("EP.23", "ep 7", "EP07", "第12期", "S02E07", "Vol.5", "NO.3"):
        assert textslot.is_episode_number(text), text


def test_a_title_is_not_an_episode_number() -> None:
    for text in ("《罗小黑战记2》独家专访", "六年磨一剑", "本期嘉宾", "EP 官方" ):
        assert not textslot.is_episode_number(text), text


def test_the_number_is_found_by_text_not_by_size() -> None:
    """On the sample the number (91.7 px) is **larger** than the title (75 px).

    A "biggest header layer is the title" rule would therefore put the number in the title
    slot and leave the actual title classified as a note.
    """
    slots = textslot.classify(
        [
            _candidate("EP.23", "EP.23", x=2280, y=20, size=91.7),
            _candidate("标题", "《罗小黑战记2》独家专访", x=1424, y=117, w=823, h=145, size=75.0),
        ],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
    )
    by_role = {slot.role: slot for slot in slots}
    assert by_role[textslot.ROLE_EPISODE].text == "EP.23"
    assert by_role[textslot.ROLE_TITLE].text == "《罗小黑战记2》独家专访"


def test_header_layers_without_a_number_still_produce_both_slots() -> None:
    """「最新一期」instead of a number: both stay editable rather than one being baked in."""
    slots = textslot.classify(
        [
            _candidate("A", "最新一期", x=1900, y=20, size=34.0),
            _candidate("B", "标题在这里", x=1500, y=120, size=70.0),
        ],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
    )
    roles = {slot.role for slot in slots}
    assert roles == {textslot.ROLE_EPISODE, textslot.ROLE_TITLE}


def test_a_layer_inside_an_avatar_group_is_a_name() -> None:
    slots = textslot.classify(
        [_candidate("小榕", "小榕", x=2295, y=559, size=21.2, avatar="头像1 拷贝")],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
    )
    assert [slot.key for slot in slots] == ["avatar:头像1 拷贝"]
    assert slots[0].role == textslot.ROLE_AVATAR_NAME


def test_a_layer_below_the_header_band_is_a_note() -> None:
    slots = textslot.classify(
        [_candidate("页脚", "欢迎大家关注、加群、一键三连！", x=1430, y=1374, w=340, h=48)],
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
    )
    assert slots[0].role == textslot.ROLE_NOTE
    assert slots[0].key.startswith("note:")


def test_colour_values_are_read_in_photoshop_order() -> None:
    """``[alpha, r, g, b]`` — verified against the composite, not the docs.

    ``EP.23`` reports ``[1.0, 0.098, 0.278, 0.729]`` and renders as ``#1947ba``. Reading it
    as ``[r, g, b, a]`` would give ``#001947``, a dark navy that is visibly wrong.
    """
    assert textslot.colour_from_values([1.0, 0.098, 0.278, 0.729]) == "#1947ba"
    assert textslot.colour_from_values(None) == "#000000"
    assert textslot.colour_from_values([1.0, 1.0, 0.0, 0.0]) == "#ff0000"


def test_scaled_size_applies_both_the_psd_scale_and_the_canvas_factor() -> None:
    # 25 px at 0.8493 in a 2560-wide PSD, compiled to 1920 → 16 px.
    assert textslot.scaled_size(25.0, 0.8493, 0.75) == 16
    # Never zero, even for a caption too small to see.
    assert textslot.scaled_size(1.0, 1.0, 0.1) == 6


# --- font discovery -----------------------------------------------------


def test_the_design_fonts_resolve_on_this_machine() -> None:
    """数黑 and Baigo are not installed — they live in 字由's download store.

    Skipped rather than failed on a machine without 字由: the product works with any font,
    it just does not look like the design.
    """
    for family in (fonts.TITLE_FONT, fonts.EPISODE_FONT):
        face = fonts.resolve(family)
        if face is None:
            pytest.skip(f"这台机器上没有 {family}（字由未安装或未下载）")
        assert face.path.is_file(), face.path


def test_resolve_is_case_and_space_insensitive() -> None:
    face = fonts.resolve(fonts.TITLE_FONT)
    if face is None:
        pytest.skip("数黑 not installed")
    for spelling in ("alimama shuheiti", "ALIMAMA SHUHEITI", "Alimama  ShuHeiTi"):
        assert fonts.resolve(spelling) is not None, spelling


def test_resolve_returns_none_for_a_font_that_does_not_exist() -> None:
    assert fonts.resolve("不存在的字体 ABCXYZ") is None


# --- the text overlay ---------------------------------------------------


def test_render_draws_nothing_for_an_empty_slot(tmp_path: Path) -> None:
    target = tmp_path / "empty.png"
    overlay.render(
        target=target,
        width=320,
        height=180,
        slots=[overlay.DrawSlot("", 0, 0, 100, 40, 20, "#000000", fonts.TITLE_FONT)],
    )
    assert np.array(Image.open(target))[:, :, 3].max() == 0


def test_render_puts_ink_inside_the_slot_rect(tmp_path: Path) -> None:
    face = fonts.resolve(fonts.TITLE_FONT)
    if face is None:
        pytest.skip("数黑 not installed")
    target = tmp_path / "one.png"
    overlay.render(
        target=target,
        width=400,
        height=200,
        slots=[overlay.DrawSlot("标题", 40, 60, 300, 60, 40, "#1947ba", fonts.TITLE_FONT)],
    )
    alpha = np.array(Image.open(target))[:, :, 3]
    ys, xs = np.nonzero(alpha)
    assert len(xs) > 0
    assert xs.min() >= 40 and xs.max() <= 340
    assert ys.min() >= 55 and ys.max() <= 130


def test_a_long_title_shrinks_instead_of_overflowing(tmp_path: Path) -> None:
    """The rect is the design; the size gives way, not the box.

    The width is checked as well as the height because the wrapper may return a line slightly
    wider than it was handed — it will not break before 「》」— so a title can fit vertically
    and still run off the edge. Measured before the fix: a 200 px slot produced a 224 px line.
    """
    face = fonts.resolve(fonts.TITLE_FONT)
    if face is None:
        pytest.skip("数黑 not installed")
    target = tmp_path / "long.png"
    long_text = "《罗小黑战记2》独家专访六年磨一剑背后的苦与乐" * 3
    overlay.render(
        target=target,
        width=400,
        height=400,
        slots=[overlay.DrawSlot(long_text, 20, 20, 200, 300, 40, "#000000", fonts.TITLE_FONT)],
    )
    alpha = np.array(Image.open(target))[:, :, 3]
    ys, xs = np.nonzero(alpha)
    assert xs.max() <= 226, f"text ran to x={xs.max()}, past the 220 edge"
    assert ys.max() <= 326, f"text ran to y={ys.max()}, past the 320 edge"


def test_signature_changes_with_the_text_and_not_with_the_rect() -> None:
    a = [overlay.DrawSlot("标题", 0, 0, 100, 40, 20, "#000000", fonts.TITLE_FONT)]
    b = [overlay.DrawSlot("副标题", 0, 0, 100, 40, 20, "#000000", fonts.TITLE_FONT)]
    assert overlay.signature(a) != overlay.signature(b)
    assert overlay.signature(a) == overlay.signature(list(a))


def test_missing_font_raises_a_503_naming_the_problem(tmp_path: Path) -> None:
    with pytest.raises(AppError) as caught:
        overlay.render(
            target=tmp_path / "x.png",
            width=100,
            height=100,
            slots=[overlay.DrawSlot("x", 0, 0, 50, 20, 16, "#000000", "绝对没有这个字体")],
        )
    assert caught.value.code in {"font_missing", "text_layout_failed"}
    assert caught.value.status == 503


# --- the real compile ---------------------------------------------------


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("textslot-build")
    return compile_template(SAMPLE_PSD, out)


def test_the_sample_produces_the_slots_the_design_expects(compiled) -> None:
    roles = {slot.role: slot for slot in compiled.texts}
    assert roles[textslot.ROLE_EPISODE].text == "EP.23"
    assert roles[textslot.ROLE_TITLE].text.startswith("《罗小黑战记2》独家专访")
    assert roles[textslot.ROLE_EPISODE].font == fonts.EPISODE_FONT
    assert roles[textslot.ROLE_TITLE].font == fonts.TITLE_FONT
    names = [slot for slot in compiled.texts if slot.role == textslot.ROLE_AVATAR_NAME]
    assert len(names) == 4
    assert {slot.text for slot in names} == {"小榕", "呆尾", "马小褂", "彼方"}
    for slot in names:
        assert slot.font == fonts.NAME_FONT  # 数黑, per the design


def _ink_pixels(image: Image.Image, rect, *, threshold: int = 60) -> int:
    """Pixels far from the crop's own median colour — i.e. glyph ink, not background."""
    crop = np.array(image.crop((rect.x, rect.y, rect.right, rect.bottom)))[:, :, :3]
    flat = crop.reshape(-1, 3).astype(int)
    median = np.median(flat, axis=0)
    return int((np.abs(flat - median).sum(axis=1) > threshold).sum())


def test_every_text_layer_left_the_background(compiled) -> None:
    """Nine type layers in the PSD, **zero ink** left behind in the poster.

    Measured, not assumed: for each slot's rect, count the pixels that differ from that
    region's median colour. Before this change the same measurement read 39,549 ink pixels
    across the nine rects (36,774 in the title's box alone); it is now 0 in every one.

    This is the assertion that actually protects the feature. A name left baked into the
    poster would show **two** names on the film — the editable one and the one underneath —
    and no amount of correct API plumbing would make that visible in a test that only checked
    that the slot list was populated.
    """
    assert len(compiled.texts) == 9
    flattened = Image.open(compiled.background_bare_path).convert("RGBA")
    leftovers = {
        slot.key: _ink_pixels(flattened, slot.rect)
        for slot in compiled.texts
    }
    assert leftovers == dict.fromkeys(leftovers, 0), leftovers


def test_the_psd_shows_ink_where_the_slots_are(compiled) -> None:
    """The control for the test above: the same rects *do* contain ink in the PSD.

    Without this, "0 ink" would also pass on an all-white poster, or on slots whose rects
    pointed at empty space — and the previous test would be measuring nothing.
    """
    psd = PSDImage.open(SAMPLE_PSD)
    reference = psd.composite(ignore_preview=True).convert("RGBA").resize(
        (compiled.canvas_w, compiled.canvas_h), Image.Resampling.LANCZOS
    )
    inked = [slot.key for slot in compiled.texts if _ink_pixels(reference, slot.rect) > 100]
    missing = {slot.key for slot in compiled.texts} - set(inked)
    assert len(inked) == len(compiled.texts), f"no ink found for {missing}"


def test_each_avatar_has_a_frame_and_a_mask(compiled) -> None:
    """Both files exist, at the **PSD's** resolution.

    The compiled avatar art is written before the canvas factor is applied — measured 236×249
    for a slot whose 1920-space rect is 177×187 — and ffmpeg scales it into place. That is
    deliberate (it keeps the ring sharp on a 4K export) and it is why ``fit_photo`` reads the
    size from the frame rather than from ``rect``.
    """
    for slot in compiled.avatars:
        frame = Image.open(slot.frame_path)
        mask = Image.open(slot.mask_path)
        assert frame.size == mask.size, slot.name
        # Larger than the 1920-space rect, because the art is at the PSD's 2560 scale.
        assert frame.size[0] >= slot.rect.w, slot.name
        assert slot.mask_rect.w > 0 and slot.mask_rect.h > 0, slot.name
        # The mask is the visible photo after the ring clips it, so it is smaller than the
        # group's own box.
        assert slot.mask_rect.w <= frame.size[0], slot.name


def test_the_frame_has_no_photo_and_no_name(compiled) -> None:
    """The split's whole point: what is left is the ring, so a replacement can go behind it.

    Two properties, and they are different:

    * the **hole** must be transparent — measured alpha 0 at its centre, where the lit PNG has
      an opaque pixel. Otherwise a replacement is composited over an opaque plate and never
      shows;
    * the name's box must hold **no ink** — the plate behind it is opaque white, so alpha
      cannot answer this one; the glyphs are gone if every pixel there is that same white.
    """
    slot = compiled.avatars[0]
    frame = Image.open(slot.frame_path).convert("RGBA")
    mask = Image.open(slot.mask_path).convert("L")
    assert frame.size == mask.size

    cx = slot.mask_rect.x + slot.mask_rect.w // 2
    cy = slot.mask_rect.y + slot.mask_rect.h // 2
    assert int(np.array(frame)[cy, cx, 3]) < 40, "the hole is opaque; a replacement would not show"

    lit = Image.open(slot.lit_path).convert("RGBA")
    assert _ink_pixels(frame, slot.mask_rect) < _ink_pixels(lit, slot.mask_rect)

    name_box = Rect(x=6, y=3, w=40, h=20)  # measured: 「呆尾」 at (6,3)-(46,23) in-group
    assert _ink_pixels(frame, name_box) == 0, "the avatar name is still baked into the frame"


def test_fit_photo_replaces_the_face_and_keeps_the_ring(compiled, tmp_path: Path) -> None:
    slot = compiled.avatars[0]
    source = tmp_path / "face.png"
    Image.new("RGB", (128, 128), (0, 200, 0)).save(source)
    target = tmp_path / "out.png"
    overlay.fit_photo(
        source=source,
        frame=Path(slot.frame_path),
        mask=Path(slot.mask_path),
        mask_rect=(
            slot.mask_rect.x,
            slot.mask_rect.y,
            slot.mask_rect.w,
            slot.mask_rect.h,
        ),
        target=target,
    )
    result = np.array(Image.open(target).convert("RGBA"))
    frame = np.array(Image.open(slot.frame_path).convert("RGBA"))
    # The middle of the hole is the new photo.
    cx = slot.mask_rect.x + slot.mask_rect.w // 2
    cy = slot.mask_rect.y + slot.mask_rect.h // 2
    assert tuple(result[cy, cx][:3]) == (0, 200, 0)
    # The ring is untouched.
    edge = np.array_equal(result[0], frame[0])
    assert edge


def test_the_psd_really_has_the_layers_we_claim() -> None:
    """A guard against the fixture: nine *reachable* type layers, four avatar groups.

    ``descendants()`` reports 14 type layers with ``visible`` set, because four of them sit
    inside the four **hidden** avatar groups — their own flag is on, their parent's is not.
    The compile walks the tree and never enters a hidden group, which is why it finds nine.
    Both numbers are pinned: if a future ``psd-tools`` changed either, the difference would
    show up here rather than as text silently vanishing from the poster.
    """
    psd = PSDImage.open(SAMPLE_PSD)
    all_types = [layer for layer in psd.descendants() if layer.kind == "type" and layer.visible]
    assert len(all_types) == 14

    reachable: list[object] = []
    stack: list[object] = [layer for layer in psd if layer.visible]
    while stack:
        layer = stack.pop()
        if layer.is_group():
            stack.extend(child for child in layer if child.visible)
        elif layer.kind == "type":
            reachable.append(layer)
    assert len(reachable) == 9

    groups = [
        layer for layer in psd if layer.is_group() and layer.visible and layer.name.startswith("头像")
    ]
    assert len(groups) == 4
