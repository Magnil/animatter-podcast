"""A multi-line slot keeps the colours of its later lines (§19.12, §19.14).

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
