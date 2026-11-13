"""Slide PNG rendering with PIL (§19.6).

Deterministic on purpose: same project → same film is a product promise, and the user
re-exports to compare. That is why this is PIL and not headless Chrome — measured 87 ms per
frame for Chrome and no guarantee of identical pixels across GPU/driver versions.

The layout arithmetic lives in :mod:`animatter.core.slide` and is tested with a fake font
there; this module only knows about files, fonts and pixels.

Title and body take **separate families** (round 101). A deck's page is a title and a list of
points, and those two are different kinds of text; the template tab has set them separately
since §19.13, so a deck that could not would be the one place in the product where type is
all-or-nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from animatter.core.slide import SlideLayout, layout
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import fonts

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

#: Default families. Deliberately the same family as the subtitles: two different CJK faces in
#: one film looks like an accident. Both are overridable per deck (``deck.title_font`` /
#: ``deck.body_font``), and the title defaults to the same one so an untouched deck looks
#: exactly as it did before fonts were settable.
FONT_FAMILY: Final[str] = "Microsoft YaHei"

LAYOUTS: Final[tuple[str, ...]] = ("title", "title_body", "quote")


@dataclass(frozen=True)
class RenderedSlide:
    path: Path
    width: int
    height: int


def _font_path(family: str) -> Path:
    """The file to draw ``family`` with, or a 503 naming what *is* available.

    Resolved through :mod:`animatter.media.fonts`, not through
    ``subtitle_ass.find_font``: the latter only scans ``C:/Windows/Fonts`` and therefore
    reports 数黑 and Baigo — the two families this product's own templates use, which live in
    字由's download store — as missing. Offering a font picker whose choices cannot be drawn
    would be worse than offering none.
    """
    face = fonts.resolve(family)
    if face is not None and face.path.is_file():
        return face.path
    installed = "、".join(fonts.families()[:10]) or "（一个都没找到）"
    raise AppError(
        "slide_font_missing",
        f"找不到幻灯片要用的字体「{family}」",
        hint=f"装一个中文字体（这台机器上有：{installed}），或把演示稿的字体改成已有的。",
        status=503,
    )


def _load(family: str, size: int) -> ImageFont.FreeTypeFont:
    """A ``FreeTypeFont`` at one size, cached by Pillow's own ``ImageFont`` internals.

    ``index=0`` matters for ``.ttc`` collections: ``msyh.ttc`` holds several faces and the
    default index is not guaranteed to be the Regular one, which would silently give the
    deck a different weight from the subtitles.
    """
    return ImageFont.truetype(str(_font_path(family)), size=size, index=0)


def measure_with(font_size: int) -> ImageFont.FreeTypeFont:
    """The default family at one size — a diagnostic hook, not a layout path."""
    return _load(FONT_FAMILY, font_size)


def render(
    *,
    target: Path,
    title: str,
    body: str,
    layout_kind: str = "title_body",
    width: int = DEFAULT_W,
    height: int = DEFAULT_H,
    title_font: str = FONT_FAMILY,
    body_font: str = FONT_FAMILY,
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

    # Two caches, keyed by (family, size): the title and the body can be different families,
    # so a size alone no longer identifies a face.
    cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def font_for(family: str, size: int) -> ImageFont.FreeTypeFont:
        key = (family, size)
        font = cache.get(key)
        if font is None:
            font = _load(family, size)
            cache[key] = font
        return font

    def measure(text: str, size: int) -> float:
        return float(font_for(body_font, size).getlength(text))

    def measure_title(text: str, size: int) -> float:
        return float(font_for(title_font, size).getlength(text))

    if layout_kind == "quote":
        plan = layout(
            title="", body=body, width=text_w, height=text_h, measure=measure,
            max_body_px=72, min_body_px=30,
        )
    elif layout_kind == "title":
        plan = layout(
            title=title, body="", width=text_w, height=text_h, measure=measure,
            title_measure=measure_title, max_body_px=40, min_body_px=24,
        )
    else:
        plan = layout(
            title=title, body=body, width=text_w, height=text_h,
            measure=measure, title_measure=measure_title,
        )

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    if layout_kind == "quote":
        _draw_quote(draw, plan, font_for(body_font, plan.body_px), pad=pad, height=height,
                    reserve=reserve)
    elif layout_kind == "title":
        _draw_title_only(
            draw, plan, font_for(title_font, plan.title_px),
            pad=pad, width=width, height=height, reserve=reserve,
        )
    else:
        _draw_title_body(
            draw, plan,
            title_font=font_for(title_font, plan.title_px),
            body_font=font_for(body_font, plan.body_px),
            pad=pad,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    return RenderedSlide(path=target, width=width, height=height)


def _draw_title_body(
    draw: ImageDraw.ImageDraw,
    plan: SlideLayout,
    *,
    title_font: ImageFont.FreeTypeFont,
    body_font: ImageFont.FreeTypeFont,
    pad: int,
) -> None:
    y = pad
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
    y = pad + plan.body_top_px
    for line in plan.body_lines:
        draw.text((pad, y), line, font=body_font, fill=BODY_COLOUR)
        y += plan.leading_px


def _draw_title_only(
    draw: ImageDraw.ImageDraw,
    plan: SlideLayout,
    font: ImageFont.FreeTypeFont,
    *,
    pad: int,
    width: int,
    height: int,
    reserve: int,
) -> None:
    """A section break: the title alone, centred in the space the subtitle leaves."""
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
    font: ImageFont.FreeTypeFont,
    *,
    pad: int,
    height: int,
    reserve: int,
) -> None:
    """A pull quote: a thick accent rule on the left, centred above the subtitle."""
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
    return {family: (font, ) for family, font in ((FONT_FAMILY, str(_font_path(FONT_FAMILY))),)}

