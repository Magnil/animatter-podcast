"""Slide PNG rendering with PIL (§19.6).

Deterministic on purpose: same project → same film is a product promise, and the user
re-exports to compare. That is why this is PIL and not headless Chrome — measured 87 ms per
frame for Chrome and no guarantee of identical pixels across GPU/driver versions.

The layout arithmetic lives in :mod:`animatter.core.slide` and is tested with a fake font
there; this module only knows about files, fonts and pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from animatter.core.slide import SlideLayout, layout
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media.subtitle_ass import (
    _FONT_DIRS,
    _FONT_FILES,
    find_font,
)

log = get_logger(__name__)

#: Slides are rendered at the media box's exact pixel size, so they are never rescaled on the
#: way into the film. The box on the sample template is 1440×810.
DEFAULT_W: Final[int] = 1440
DEFAULT_H: Final[int] = 810

#: Inner padding, as a fraction of the shorter side. 6 % of 810 is 49 px, which reads as a
#: deliberate margin at this size rather than a rendering accident.
PADDING_RATIO: Final[float] = 0.06

#: Extra space kept empty at the bottom for the burned-in subtitle, as a fraction of the
#: box height. Subtitles are drawn at the frame's own `margin_v` (56 px of 1080) and know
#: nothing about what is inside the box, so on a real frame the last body line ended ~10 px
#: above the subtitle's cap height — text sitting on text. Reserving 9 % of the box height
#: (73 px of 810) turns that into a visible gap and costs half a line of body.
SUBTITLE_RESERVE_RATIO: Final[float] = 0.09

#: The deck's own palette. Deliberately not the template's: a slide is content, and the
#: template's cream poster is the *frame* around it. Mismatching them is what makes a slide
#: look pasted on.
BACKGROUND: Final[tuple[int, int, int]] = (255, 255, 255)
TITLE_COLOUR: Final[tuple[int, int, int]] = (26, 32, 44)
BODY_COLOUR: Final[tuple[int, int, int]] = (52, 58, 70)
ACCENT: Final[tuple[int, int, int]] = (46, 84, 168)
QUOTE_COLOUR: Final[tuple[int, int, int]] = (96, 104, 120)

#: Font family for slides. Deliberately the same family as the subtitles: two different
#: CJK faces in one film looks like an accident.
FONT_FAMILY: Final[str] = "Microsoft YaHei"

LAYOUTS: Final[tuple[str, ...]] = ("title", "title_body", "quote")


@dataclass(frozen=True)
class RenderedSlide:
    path: Path
    width: int
    height: int


def _font_path() -> Path:
    path = find_font(FONT_FAMILY)
    if path is None:
        stems = {
            item.stem
            for directory in _FONT_DIRS
            if directory.is_dir()
            for item in directory.iterdir()
            if item.suffix.lower() in {".ttf", ".ttc", ".otf"}
        }
        installed = "、".join(sorted(stems)[:8]) or "（一个都没找到）"
        raise AppError(
            "slide_font_missing",
            f"找不到幻灯片要用的字体「{FONT_FAMILY}」",
            hint=f"装一个中文字体（这台机器上有：{installed}），或把 deck 的字体改成已有的。",
            status=503,
        )
    return path


def _load(size: int) -> ImageFont.FreeTypeFont:
    """A ``FreeTypeFont`` at one size, cached by Pillow's own ``ImageFont`` internals.

    ``index=0`` matters for ``.ttc`` collections: ``msyh.ttc`` holds several faces and the
    default index is not guaranteed to be the Regular one, which would silently give the
    deck a different weight from the subtitles.
    """
    return ImageFont.truetype(str(_font_path()), size=size, index=0)


def measure_with(font_size: int) -> ImageFont.FreeTypeFont:
    return _load(font_size)


def render(
    *,
    target: Path,
    title: str,
    body: str,
    layout_kind: str = "title_body",
    width: int = DEFAULT_W,
    height: int = DEFAULT_H,
) -> RenderedSlide:
    """Draw one page and write it as PNG.

    ``layout_kind`` selects how much of the page the artwork uses:

    * ``title`` — the title alone, vertically centred. Used for section breaks.
    * ``title_body`` — title at the top, body below. The normal page.
    * ``quote`` — the body set large with a left rule, no title. For a single striking line.
    """
    if layout_kind not in LAYOUTS:
        raise AppError(
            "slide_layout_invalid",
            f"未知的版式 {layout_kind}",
            hint=f"可选：{'、'.join(LAYOUTS)}。",
        )

    pad = int(min(width, height) * PADDING_RATIO)
    reserve = int(height * SUBTITLE_RESERVE_RATIO)
    text_w = width - pad * 2
    text_h = height - pad * 2 - reserve

    fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def measure(text: str, size: int) -> float:
        # Cached per size: laying out one page measures the same text dozens of times while
        # shrinking, and every uncached call re-opens the font file.
        font = fonts.get(size)
        if font is None:
            font = _load(size)
            fonts[size] = font
        return float(font.getlength(text))

    if layout_kind == "quote":
        plan = layout(
            title="", body=body, width=text_w, height=text_h, measure=measure,
            max_body_px=72, min_body_px=30,
        )
    elif layout_kind == "title":
        plan = layout(
            title=title, body="", width=text_w, height=text_h, measure=measure,
            max_body_px=40, min_body_px=24,
        )
    else:
        plan = layout(title=title, body=body, width=text_w, height=text_h, measure=measure)

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    if layout_kind == "quote":
        _draw_quote(draw, plan, fonts, pad=pad, height=height, reserve=reserve)
    elif layout_kind == "title":
        _draw_title_only(
            draw, plan, fonts, pad=pad, width=width, height=height, reserve=reserve
        )
    else:
        _draw_title_body(draw, plan, fonts, pad=pad)

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    return RenderedSlide(path=target, width=width, height=height)


def _draw_title_body(
    draw: ImageDraw.ImageDraw,
    plan: SlideLayout,
    fonts: dict[int, ImageFont.FreeTypeFont],
    *,
    pad: int,
) -> None:
    y = pad
    title_font = fonts.setdefault(plan.title_px, _load(plan.title_px))
    title_line_h = int(plan.title_px * 1.45)
    for line in plan.title_lines:
        draw.text((pad, y), line, font=title_font, fill=TITLE_COLOUR)
        y += title_line_h
    # The accent rule is what makes a generated page look designed rather than dumped. It
    # sits between the title and the body, and is sized off the body font so it scales.
    if plan.title_lines:
        rule_y = y + int(plan.body_px * 0.35)
        draw.rectangle(
            [pad, rule_y, pad + int(plan.body_px * 3.2), rule_y + max(3, plan.body_px // 12)],
            fill=ACCENT,
        )
    body_font = fonts.setdefault(plan.body_px, _load(plan.body_px))
    y = pad + plan.body_top_px
    for line in plan.body_lines:
        draw.text((pad, y), line, font=body_font, fill=BODY_COLOUR)
        y += plan.leading_px


def _draw_title_only(
    draw: ImageDraw.ImageDraw,
    plan: SlideLayout,
    fonts: dict[int, ImageFont.FreeTypeFont],
    *,
    pad: int,
    width: int,
    height: int,
    reserve: int,
) -> None:
    """A section break: the title alone, centred in the space the subtitle leaves."""
    font = fonts.setdefault(plan.title_px, _load(plan.title_px))
    line_h = int(plan.title_px * 1.45)
    block_h = len(plan.title_lines) * line_h
    y = (height - reserve - block_h) // 2
    for line in plan.title_lines:
        line_w = draw.textlength(line, font=font)
        draw.text(((width - line_w) / 2, y), line, font=font, fill=TITLE_COLOUR)
        y += line_h
    rule_y = y + int(plan.title_px * 0.25)
    rule_w = int(plan.title_px * 4)
    draw.rectangle(
        [(width - rule_w) // 2, rule_y, (width + rule_w) // 2, rule_y + max(4, plan.title_px // 10)],
        fill=ACCENT,
    )


def _draw_quote(
    draw: ImageDraw.ImageDraw,
    plan: SlideLayout,
    fonts: dict[int, ImageFont.FreeTypeFont],
    *,
    pad: int,
    height: int,
    reserve: int,
) -> None:
    """A pull quote: a thick accent rule on the left, centred above the subtitle."""
    font = fonts.setdefault(plan.body_px, _load(plan.body_px))
    block_h = len(plan.body_lines) * plan.leading_px
    y = max(pad, (height - reserve - block_h) // 2)
    left = pad + int(plan.body_px * 1.2)
    rule_w = max(6, plan.body_px // 8)
    draw.rectangle([pad, y, pad + rule_w, y + block_h], fill=ACCENT)
    for line in plan.body_lines:
        draw.text((left, y), line, font=font, fill=QUOTE_COLOUR)
        y += plan.leading_px


def font_table() -> dict[str, tuple[str, ...]]:
    """Exposed for the same reason the subtitle module has one: tests and diagnostics."""
    return dict(_FONT_FILES)
