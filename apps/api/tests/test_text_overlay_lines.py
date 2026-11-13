"""A multi-line slot keeps the colours of its later lines (§19.12, §19.13).

The sample title is two paragraphs, and the PSD paints one phrase on the **second** line red:

    《罗小黑战记2》**独家专访**
    六年磨一剑背后的**苦与乐**

Measured on a real still before the fix: line 1 held 4,618 red pixels and line 2 held **0** —
「苦与乐」 came out blue. The cause was neither the markup nor the parser: the wrapper splits the
text on ``\\n``, so a paragraph break reaches the colour mapping as a line boundary with **no
newline character of its own**, and the skip-whitespace step explicitly excluded ``\\n``. Every
line after the first then "disagreed" and the fallback painted it entirely in the base colour —
a wrong picture with no error anywhere.

This test goes through :func:`text_overlay.render`, the function that actually writes the PNG:
counting colours on the output cannot pass while the mapping is wrong.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from animatter.core import textspan
from animatter.media import fonts
from animatter.media import text_overlay as overlay

TITLE = "《罗小黑战记2》**独家专访**\n六年磨一剑背后的**苦与乐**"


def _counts(image_path: Path) -> dict[str, dict[str, int]]:
    """Red / blue ink per **text line**, split at the blank band between them.

    Two mistakes this helper made and now cannot:

    * splitting the canvas in half by height. The two lines occupy y=0..108 of a 200 px canvas,
      so both fell in the upper half and "line 2" measured an empty region.
    * finding "inked" rows by darkness on an RGB copy of an RGBA image. Every transparent pixel
      converts to black, so the whole canvas read as inked and there was no gap to split on.
      Alpha is the only honest test for "did anything get drawn here".
    """
    image = np.array(Image.open(image_path).convert("RGBA")).astype(int)
    inked = (image[:, :, 3] > 8).any(axis=1)
    rows = np.nonzero(inked)[0]
    assert len(rows) > 0, "nothing was drawn"
    gaps = [y for y in range(int(rows.min()), int(rows.max())) if not inked[y]]
    if not gaps:
        # A one-paragraph slot has no gap to split on — that is the control case, not a failure.
        bands = {"line1": (int(rows.min()), int(rows.max()) + 1)}
    else:
        split = gaps[len(gaps) // 2]
        bands = {"line1": (int(rows.min()), split), "line2": (split, int(rows.max()) + 1)}
    out: dict[str, dict[str, int]] = {}
    for name, (top, bottom) in bands.items():
        band = image[top:bottom, :, :3].reshape(-1, 3)
        counts: dict[str, int] = {}
        for colour, value in (("red", textspan.RED), ("blue", textspan.BLUE)):
            target = np.array([int(value[index : index + 2], 16) for index in (1, 3, 5)])
            counts[colour] = int((np.abs(band - target).sum(axis=1) < 30).sum())
        out[name] = counts
    return out


def test_the_second_line_keeps_its_accent(tmp_path: Path) -> None:
    if fonts.resolve(fonts.TITLE_FONT) is None:  # pragma: no cover - depends on the machine
        pytest.skip("数黑 not installed")

    target = tmp_path / "title.png"
    overlay.render(
        target=target,
        width=900,
        height=200,
        # 40 px over two lines of ~54 px, so each paragraph owns one half of the canvas.
        slots=[overlay.DrawSlot(TITLE, 0, 0, 900, 200, 40, textspan.BLUE, fonts.TITLE_FONT)],
    )

    counts = _counts(target)
    # Line 1 is already two colours in the PSD; line 2 is the one the bug flattened.
    assert counts["line1"]["red"] > 100, counts
    assert counts["line1"]["blue"] > 100, counts
    assert counts["line2"]["red"] > 100, counts
    assert counts["line2"]["blue"] > 100, counts


def test_a_single_line_slot_is_unchanged(tmp_path: Path) -> None:
    """The control: one paragraph was always correct, and must stay so."""
    if fonts.resolve(fonts.EPISODE_FONT) is None:  # pragma: no cover - depends on the machine
        pytest.skip("Baigo not installed")

    target = tmp_path / "episode.png"
    overlay.render(
        target=target,
        width=400,
        height=120,
        slots=[
            overlay.DrawSlot(
                "EP.**23**", 0, 0, 400, 120, 60, textspan.BLUE, fonts.EPISODE_FONT
            )
        ],
    )
    counts = _counts(target)
    assert counts["line1"]["red"] > 50, counts
    assert counts["line1"]["blue"] > 50, counts


# --- the fitting loop must always run -----------------------------------


def _slot(*, text: str, font_size: int, w: int = 200, h: int = 50, wrap: bool = False) -> overlay.DrawSlot:
    return overlay.DrawSlot(
        text=text,
        x=0,
        y=0,
        w=w,
        h=h,
        font_size=font_size,
        colour="#000000",
        font=fonts.TITLE_FONT,
        wrap=wrap,
    )


def test_a_slot_with_no_recorded_size_still_draws() -> None:
    """A `font_size` of 0 must render tiny, not kill the export.

    `slots_json` written before the compiler recorded per-slot sizes has no `font_size`, so
    `draw_slots` passes 0. `floor = max(12, 0)` then exceeded the start size, the fitting loop
    never ran, and the export failed with 「这段文字排不下」 — after the audio render had
    already run. Measured on a template compiled 2026-11-10; a template compiled later works,
    which is why it went unnoticed.
    """
    slot = _slot(text="EP.23", font_size=0)
    _font, lines, size = overlay._fit(slot, base_size=0)
    assert lines == ["EP.23"]
    # Falls back to the slot's height, not to the 6 px floor: 6 px is unreadable, and this rect
    # is 50 px tall, so anything near it is the only legible answer.
    assert size > 20, size


def test_a_slot_with_no_recorded_size_ends_up_inside_its_rect() -> None:
    """The fallback may shrink past the authored 0.5 floor: there is no intent to protect.

    With the floor kept, a 617×109 title fell back to 109 px, wrapped to three lines and came
    out 194 px tall — overflowing the rect it was supposed to fill.
    """
    slot = _slot(
        text="《罗小黑战记2》独家专访\n六年磨一剑背后的苦与乐",
        font_size=0,
        w=617,
        h=109,
        wrap=True,
    )
    font, lines, size = overlay._fit(slot, base_size=0)
    total = overlay._line_height(font, size) * len(lines)
    assert total <= slot.h, (size, len(lines), total)


def test_a_single_digit_slot_is_drawn_at_its_own_size() -> None:
    """The old floor was 12, but `scaled_size` documents and produces 6 px."""
    _font, _lines, size = overlay._fit(_slot(text="彼方", font_size=8, w=30, h=16), base_size=8)
    assert size == 8


def test_the_fitting_loop_always_runs_at_least_once() -> None:
    """`floor` may never exceed the start size — that was the bug, not the floor itself.

    Before the fix, `floor = max(12, round(size * 0.5))` skipped the loop for every slot under
    12 px and left `best is None`, i.e. 「这段文字排不下」 for a slot the UI listed as editable.
    The loop running is what matters; a caller asking for 1 px still gets 1 px.
    """
    for size in (1, 6, 8, 11, 12, 13, 56):
        _font, _lines, chosen = overlay._fit(
            _slot(text="测", font_size=size), base_size=size
        )
        assert 1 <= chosen <= size, (size, chosen)
