"""Render tests for multi-colour slots: both colours must actually reach the pixels."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from animatter.core import textspan
from animatter.media import fonts
from animatter.media import text_overlay as overlay


def _render(tmp_path: Path, slot: overlay.DrawSlot) -> Image.Image:
    target = tmp_path / "one.png"
    overlay.render(target=target, width=400, height=200, slots=[slot])
    return Image.open(target).convert("RGBA")


def _count(image: Image.Image, red: int, green: int, blue: int, *, tolerance: int = 40) -> int:
    array = np.array(image).astype(int)
    mask = array[:, :, 3] > 200
    pixels = array[mask][:, :3]
    if not len(pixels):
        return 0
    close = (
        (np.abs(pixels[:, 0] - red) < tolerance)
        & (np.abs(pixels[:, 1] - green) < tolerance)
        & (np.abs(pixels[:, 2] - blue) < tolerance)
    )
    return int(close.sum())


def test_a_marked_slot_draws_both_colours(tmp_path: Path) -> None:
    """The point of the markup: a pixel test, not a string test.

    Blue ``#1947ba`` and red ``#f9485c`` are far enough apart that no antialiasing blend lands
    within 40 of both, so seeing both means both colours were really painted.
    """
    if fonts.resolve(fonts.TITLE_FONT) is None:
        pytest.skip("数黑 not installed")
    image = _render(
        tmp_path,
        overlay.DrawSlot(
            text="EP.**23**",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=64,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    assert _count(image, 0x19, 0x47, 0xBA) > 100, "no blue ink"
    assert _count(image, 0xF9, 0x48, 0x5C) > 100, "no red ink — the accent was not painted"


def test_the_accent_is_inside_the_line_not_on_its_own(tmp_path: Path) -> None:
    """Two runs, one baseline: the red starts where the blue ended, at the same ``y``."""
    if fonts.resolve(fonts.TITLE_FONT) is None:
        pytest.skip("数黑 not installed")
    image = _render(
        tmp_path,
        overlay.DrawSlot(
            text="AB**CD**",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=64,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    array = np.array(image)
    blue_rows = np.nonzero((array[:, :, 2] > 150) & (array[:, :, 0] < 90) & (array[:, :, 3] > 200))[0]
    red_rows = np.nonzero((array[:, :, 0] > 200) & (array[:, :, 1] < 130) & (array[:, :, 3] > 200))[0]
    assert len(blue_rows) and len(red_rows)
    assert abs(int(blue_rows.min()) - int(red_rows.min())) <= 4, "the runs sit on different lines"


def test_a_plain_slot_still_draws_one_colour(tmp_path: Path) -> None:
    if fonts.resolve(fonts.TITLE_FONT) is None:
        pytest.skip("数黑 not installed")
    image = _render(
        tmp_path,
        overlay.DrawSlot(
            text="标题",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=48,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    assert _count(image, 0x19, 0x47, 0xBA) > 100
    assert _count(image, 0xF9, 0x48, 0x5C) == 0


def test_the_markers_do_not_take_up_width(tmp_path: Path) -> None:
    """``EP.**23**`` must be exactly as wide as ``EP.23`` — otherwise titles wrap earlier.

    Measured by drawing both and comparing the ink's right edge. A wrapper that measured the
    markers would push the edge out by four characters' worth of space.
    """
    if fonts.resolve(fonts.TITLE_FONT) is None:
        pytest.skip("数黑 not installed")
    marked = _render(
        tmp_path,
        overlay.DrawSlot(
            text="EP.**23**",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=64,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    plain = _render(
        tmp_path,
        overlay.DrawSlot(
            text="EP.23",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=64,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    marked_right = int(np.nonzero(np.array(marked)[:, :, 3] > 200)[1].max())
    plain_right = int(np.nonzero(np.array(plain)[:, :, 3] > 200)[1].max())
    assert abs(marked_right - plain_right) <= 2, (marked_right, plain_right)


def test_an_unpaired_marker_is_drawn_as_text(tmp_path: Path) -> None:
    """It must be visible, not swallowed: the user needs to see the typo to fix it."""
    if fonts.resolve(fonts.TITLE_FONT) is None:
        pytest.skip("数黑 not installed")
    image = _render(
        tmp_path,
        overlay.DrawSlot(
            text="EP.**23",
            x=10,
            y=40,
            w=380,
            h=120,
            font_size=40,
            colour=textspan.BLUE,
            font=fonts.TITLE_FONT,
        ),
    )
    assert _count(image, 0x19, 0x47, 0xBA) > 100
    assert _count(image, 0xF9, 0x48, 0x5C) == 0
