"""Draw the editable text onto a transparent PNG (§19.12).

Why a separate PNG instead of an ASS file
-----------------------------------------
The subtitles already go through libass, so drawing the title there would be one mechanism
instead of two. It is the wrong mechanism here:

* **Position.** An ASS cue is placed by alignment + margins inside a 16:9 canvas that libass
  may re-scale. The poster's title sits at an arbitrary rect with a left edge at x=1068 of
  1920 and a baseline that has nothing to do with any margin. Expressing that as
  ``\\pos()`` works, but every style tweak then has to be recomputed against libass's own
  idea of the canvas — and ``PlayRes`` mismatches shift *all* of it silently (§19.10 trap 14).
* **Fonts.** libass resolves a family through the OS, and the fonts this design calls for
  (数黑, Baigo) are not installed — they live in 字由's download store. Pillow can be handed
  the file directly, which is the only way to draw them at all.

So: Pillow draws one RGBA PNG the size of the canvas, and ffmpeg overlays it. The text is
static for the whole film, so one PNG costs one overlay filter and zero extra frames.

Fitting
-------
Each slot has a **rect and a font size that came from the PSD**, and the size is used as
given — the poster's design decides how big the title is, not the length of the string. What
is *not* used as given is the width: a longer title would run past the poster's edge, so the
text is wrapped to the rect and, if it still does not fit, the size shrinks in 2 px steps
down to a floor. This is the same rule ``core/slide.layout`` uses for slides, and the same
reason: overflow that nobody can see is worse than text that is a little smaller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from animatter.core import slide as slide_core
from animatter.core import textspan
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import fonts

log = get_logger(__name__)

#: A slot may shrink to this fraction of its PSD size before it stops. Half is the point at
#: which a 56 px title is still 28 px — legible on a 1080p frame — while 12 px would be a
#: caption pretending to be a title.
MIN_SCALE: Final[float] = 0.5

#: Below this, shrinking further is not worth it and the text is allowed to overflow by a
#: few pixels (visible, and therefore fixable) rather than becoming unreadable.
MIN_PX: Final[int] = 12

#: Bumped whenever a change to **this module** changes the pixels for the same slots.
#: It is part of :func:`signature`, and therefore of every still and export cache key:
#: without it, deploying a layout fix leaves the old pictures in place under unchanged
#: names, and the next request happily returns them. Measured while writing this file —
#: a wrap fix produced a byte-identical still because the digest only covered the data.
#:
#: v3: a multi-line slot keeps the colours of its **second and later** lines. Before this,
#: the newline between paragraphs broke the colour mapping and every line after the first
#: was painted in the slot's base colour (the sample title's red 「苦与乐」 was blue).
RENDER_VERSION: Final[str] = "3"


@dataclass(frozen=True)
class DrawSlot:
    """One thing to draw: where, what, how big, in what colour and face."""

    text: str
    x: int
    y: int
    w: int
    h: int
    font_size: int
    colour: str
    font: str
    #: ``left`` matches the PSD's own alignment on this template. Kept explicit so a
    #: centred slot does not have to be expressed by fudging its rect.
    align: str = "left"
    #: Whether to break the text inside ``w``. ``False`` keeps it on one line and lets it
    #: grow to the right instead — right for a 30 px name plate holding 「彼方（嘉宾）」,
    #: wrong for a title block that is meant to be two lines.
    wrap: bool = True


def _face(slot: DrawSlot) -> ImageFont.FreeTypeFont:
    """The Pillow font for a slot, or a clear error naming what is installed."""
    face = fonts.resolve(slot.font)
    if face is None:
        installed = "、".join(fonts.families()[:8]) or "（一个都没找到）"
        raise AppError(
            "font_missing",
            f"找不到字体「{slot.font}」",
            hint=(
                "在界面上换一个字体，或者把这个字体装好。"
                f"这台机器上已有：{installed}…（字由里已下载的也会被找到）"
            ),
            status=503,
        )
    return ImageFont.truetype(str(face.path), size=max(MIN_PX, slot.font_size), index=0)


def _wrap(
    text: str, *, width: int, font: ImageFont.FreeTypeFont, wrap: bool = True
) -> list[str]:
    """Wrap to ``width`` px, reusing the slide wrapper's break rules.

    Reused rather than reimplemented: 「行首不能是标点」「行尾不能是开引号」 are the same
    rules for a poster title as for a slide, and a second implementation would drift.
    """
    if not wrap:
        return text.split("\n")
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(
            slide_core.wrap(
                paragraph,
                max_width=float(width),
                measure=lambda piece: float(font.getlength(piece)),
            )
        )
    return lines


def _styled(slot: DrawSlot) -> list[tuple[str, str]]:
    """The slot's text as (character, colour) pairs, with the ``**`` markup resolved."""
    out: list[tuple[str, str]] = []
    for segment in textspan.parse(slot.text, base=slot.colour):
        out.extend((char, segment.colour) for char in segment.text)
    return out


def _colour_lines(
    styled: list[tuple[str, str]], lines: list[str], *, base: str
) -> list[list[tuple[str, str]]]:
    """Map plain wrapped ``lines`` back onto ``styled``.

    Wrapping only ever drops whitespace *between* lines — and a paragraph break is exactly one
    of those: ``_wrap`` splits the text on ``\\n`` and emits the two paragraphs as separate
    lines with no newline character of their own. So ``\\n`` has to be consumed like any other
    whitespace. Leaving it out of that test was a real bug: the second line of the title never
    matched, fell into the "disagree ⇒ paint the whole line in the base colour" branch, and the
    PSD's red 「苦与乐」 came out blue on every still. Measured on the sample poster: line 2 held
    0 red pixels instead of 2,400.

    A genuine mismatch means the two disagree (a future wrapping rule that rewrites text, say);
    that line is then painted in the slot's base colour rather than mis-painted with every
    colour shifted by one character.
    """
    out: list[list[tuple[str, str]]] = []
    position = 0
    for line in lines:
        target = line.rstrip()
        chunk: list[tuple[str, str]] = []
        index = 0
        ok = True
        while index < len(target):
            if position >= len(styled):
                ok = False
                break
            char, colour = styled[position]
            if char != target[index]:
                if char.isspace():
                    position += 1
                    continue
                ok = False
                break
            chunk.append((char, colour))
            position += 1
            index += 1
        # The newline that ended this paragraph is still ahead of us; step over it so the next
        # line starts at its own first character.
        while position < len(styled) and styled[position][0].isspace():
            position += 1
        out.append(chunk if ok else [(char, base) for char in target])
    return out


def _runs(line: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Collapse a coloured line into stretches of one colour."""
    runs: list[tuple[str, str]] = []
    for char, colour in line:
        if runs and runs[-1][1] == colour:
            runs[-1] = (runs[-1][0] + char, colour)
        else:
            runs.append((char, colour))
    return runs


def _line_height(font: ImageFont.FreeTypeFont, size: int) -> int:
    """One line's advance, in pixels.

    Measured from the font rather than assumed at ``size * 1.2``: CJK faces differ by ~15 %
    in default leading, and on a title block of three lines that is half a line of overflow.
    """
    ascent, descent = font.getmetrics()
    return round((ascent + descent) * 1.02) or int(size * 1.2)


def _fit(
    slot: DrawSlot, *, base_size: int, max_width: int | None = None
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Largest size (down to ``MIN_SCALE``) whose wrapped block fits the slot's rect.

    **Both** dimensions are checked. Checking only the height was a real bug: the wrapper
    refuses to start a line with 「》」or 「记」-style punctuation clusters, so a line can come
    back slightly *wider* than the limit it was given, and a title that fits vertically still
    ran off the poster's edge. Measured on a 200 px slot: one line wrapped to 224 px.

    At the floor the best-scoring candidate is used even if it still overflows. Overflowing by
    a few pixels is visible and fixable; shrinking a 56 px title to 9 px to make it fit is not
    a fix, and silently dropping characters is worse than either.
    """
    # A slot whose recorded size is 0 or single-digit still has to be *drawn*: `floor` may
    # never exceed the size we start from, or the loop below never runs and the export dies
    # with 「这段文字排不下」 — naming neither the slot nor the reason (round 110, found on a
    # template compiled before `slots_json` carried `font_size`).
    #
    # A missing size falls back to the slot's own **height**, not to the 6 px floor: measured on
    # that template, 6 px produced a title 6 px tall inside a 617×109 rect — an export that
    # "succeeds" with text nobody can read. Starting from the height lets the loop that follows
    # do what it already does (shrink until both dimensions fit), which is what a person would
    # do by hand, and it is the only start that can be right for both a 200×50 episode number
    # and a 617×109 two-line title.
    missing = base_size <= 0
    if missing:
        log.warning("text.size_missing", slot=slot.text[:40], w=slot.w, h=slot.h)
        base_size = max(MIN_PX, slot.h)
    size = base_size
    # The 0.5 floor protects an **authored** size from being shrunk into illegibility. A
    # missing size has nothing to protect, and keeping the floor let the fallback overflow its
    # own rect (measured: 617×109 with three lines at 54 px = 194 px tall). With no intent to
    # preserve, the goal is simply "as large as fits", so it may go down to ``MIN_PX``.
    floor = MIN_PX if missing else min(size, max(MIN_PX, round(size * MIN_SCALE)))
    path = str(fonts.require(slot.font).path)
    # A single-line slot may grow to the right; ``max_width`` is how far it can go before
    # it would leave the canvas. Falling back to the slot's own width keeps every other
    # case exactly as it was.
    limit = slot.w if max_width is None else max(slot.w, max_width)
    best: tuple[ImageFont.FreeTypeFont, list[str], int] | None = None
    best_overflow = float("inf")

    while size >= floor:
        font = ImageFont.truetype(path, size=size, index=0)
        lines = _wrap(
            textspan.plain(slot.text), width=slot.w, font=font, wrap=slot.wrap
        )
        widest = max((font.getlength(line) for line in lines), default=0.0)
        total = _line_height(font, size) * max(1, len(lines))
        overflow = max(0.0, widest - limit) + max(0.0, float(total - slot.h))
        if overflow <= 0:
            return font, lines, size
        if overflow < best_overflow:
            best_overflow = overflow
            best = (font, lines, size)
        size -= 2

    if best is None:  # pragma: no cover - the loop always assigns at the floor
        raise AppError("text_layout_failed", "这段文字排不下")
    return best


def _colour(value: str) -> tuple[int, int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return (0, 0, 0, 255)
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return (0, 0, 0, 255)
    return (red, green, blue, 255)


def render(
    *,
    target: Path,
    width: int,
    height: int,
    slots: list[DrawSlot],
) -> Path:
    """Draw every slot onto a transparent canvas and write it as PNG.

    The canvas is the **render** size, not the PSD's, so the overlay is neither scaled nor
    resampled by ffmpeg on its way into the film — a 1:1 overlay cannot drift a pixel, and
    text is exactly where a one-pixel drift is visible.
    """
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for slot in slots:
        if not slot.text.strip():
            continue
        # ``width - x`` is the room the slot has before it would run off the poster. Only
        # a non-wrapping slot is allowed to use it.
        room = width - slot.x - 4
        font, lines, size = _fit(
            slot, base_size=slot.font_size, max_width=None if slot.wrap else room
        )
        line_height = _line_height(font, size)
        painted = _colour_lines(_styled(slot), lines, base=slot.colour)
        for index, line in enumerate(lines):
            if not line:
                continue
            y = slot.y + index * line_height
            # ``x`` advances per run: the accent is *inside* the line, so each stretch is placed
            # where the previous one ended rather than all at the slot's left edge.
            width_total = font.getlength(line)
            # Explicitly float: the alignment branch divides, and an `int` annotation would make
            # that a type error rather than a centring calculation.
            x: float = float(slot.x)
            if slot.align == "center":
                x = slot.x + (slot.w - width_total) / 2
            elif slot.align == "right":
                x = slot.x + slot.w - width_total
            for run_text, run_colour in _runs(painted[index] if index < len(painted) else []):
                draw.text((x, y), run_text, font=font, fill=_colour(run_colour))
                x += font.getlength(run_text)

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    log.info("text.overlay", slots=len(slots), target=target.name, width=width, height=height)
    return target


def signature(slots: list[DrawSlot]) -> str:
    """A short hash of everything that changes these pixels.

    Used in the still-preview filename and the export signature. Both had the same bug
    before: a cached artefact outlived the change, and the symptom was "I edited the title
    and the film still shows the old one".
    """
    import hashlib

    parts = [
        f"{slot.text}|{slot.x}|{slot.y}|{slot.w}|{slot.h}|{slot.font_size}|"
        f"{slot.colour}|{slot.font}|{slot.align}|{slot.wrap}"
        for slot in slots
    ]
    parts.append(f"v{RENDER_VERSION}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:10]


def fit_photo(
    *,
    source: Path,
    frame: Path,
    mask: Path,
    mask_rect: tuple[int, int, int, int],
    target: Path,
) -> Path:
    """Composite a replacement photo into one avatar slot.

    The picture is **cover-fitted** into the mask's bounding box — scaled until it fills the
    box, cropped evenly — and then clipped by the mask, so a square photo in a round hole
    looks like the PSD's own, and a portrait photo does not stretch.

    Everything here is in the **frame's own pixel space**, taken from the frame image rather
    than from the compiled ``rect``. The compiled avatar PNGs are written at the PSD's
    resolution (236×249 for a slot whose slot rect is 177×187 at 1920) and scaled by ffmpeg
    on the way into the film; passing the slot rect in would have composited a 177-wide photo
    onto a 236-wide ring and left a transparent crescent. Reading the size from the file
    cannot drift out of step with what the compile wrote.
    """
    box_x, box_y, box_w, box_h = mask_rect
    if box_w <= 0 or box_h <= 0:
        raise AppError("avatar_mask_empty", "这个头像位没有可用的蒙版，无法换图")

    frame_img = Image.open(frame).convert("RGBA")
    mask_img = Image.open(mask).convert("L")
    photo = Image.open(source).convert("RGBA")
    group_w, group_h = frame_img.size
    if mask_img.size != frame_img.size:
        raise AppError(
            "avatar_mask_mismatch",
            "头像框和蒙版尺寸不一致",
            hint="重新编译一次模板；这两个文件必须由同一次编译产出。",
            status=409,
        )

    scale = max(box_w / photo.width, box_h / photo.height)
    scaled_w = max(1, round(photo.width * scale))
    scaled_h = max(1, round(photo.height * scale))
    photo = photo.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    left = max(0, (scaled_w - box_w) // 2)
    top = max(0, (scaled_h - box_h) // 2)
    photo = photo.crop((left, top, left + box_w, top + box_h))

    # Clip by the mask's own sub-rect, so the ring's stroke stays outside.
    mask_crop = mask_img.crop((box_x, box_y, box_x + box_w, box_y + box_h))
    clipped = Image.new("RGBA", (group_w, group_h), (0, 0, 0, 0))
    holder = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    holder.paste(photo, (0, 0))
    clipped.paste(holder, (box_x, box_y), mask_crop)

    out = frame_img.copy()
    out.alpha_composite(clipped)
    target.parent.mkdir(parents=True, exist_ok=True)
    out.save(target)
    return target


def dim(image_path: Path, *, mask_path: Path, target: Path) -> Path:
    """The "not talking" version of a replaced avatar.

    Reuses the compile's rule (desaturate + darken, alpha untouched) but applies it through
    the slot's mask, because a replacement photo is not the PSD's smart object and its pixels
    are not where ``photo_mask`` would look for them.
    """
    import numpy as np

    from animatter.media.template import _DIM_BRIGHTNESS, _DIM_DESATURATE

    image = Image.open(image_path).convert("RGBA")
    mask = np.array(Image.open(mask_path).convert("L")) > 8
    array = np.array(image).astype(np.float32)
    height, width = mask.shape
    array = array[:height, :width]
    region = array[:, :, :3]
    grey = region.mean(axis=2, keepdims=True)
    dimmed = np.clip(
        (region * (1.0 - _DIM_DESATURATE) + grey * _DIM_DESATURATE) * _DIM_BRIGHTNESS, 0, 255
    )
    array[:, :, :3] = np.where(mask[:, :, None], dimmed, region)
    Image.fromarray(array.astype(np.uint8), "RGBA").save(target)
    return target
