"""PSD template compilation (§19.2, §19.9): one pass, cached on disk.

Why a compile step at all
-------------------------
``psd-tools`` reads this file but it cannot *render* it the way Photoshop does, and
it **cannot write text at all** — ``TypeLayer.text`` raises ``AttributeError:
property 'text' ... has no setter``, and patching the low-level tag changes the
string without changing a single pixel (measured: 0.00 % pixel difference), because
the library has no glyph rasteriser.

So the PSD is treated as **artwork that is flattened once**, and every piece of text
a human may later edit is drawn by us. That decision is what makes the rest of the
feature possible; "let the user edit the PSD's own text layers" dies here.

What the compile produces
-------------------------
* ``background.png`` — the poster with the media-box plate, avatars flattened in,
  and no box content. This is what P1 renders.
* ``background_bare.png`` — the same without avatars. P3 needs it to draw
  per-speaker avatars above the box; producing both now avoids a recompile later.
* ``overlay.png`` — the layers that must sit **above** the box content. Measured on
  the sample file, the frame stroke (``内框描边``, ink bbox 311,267–2248,1366) overlaps
  the content rect (320,278–2240,1358), so it cannot live in the background.
* slot geometry — the content rect, avatar slots, and the **text slots** we draw.
* ``avatar<N>_frame.png`` / ``avatar<N>_mask.png`` — the ring without any photo, and the
  photo's own pixel mask. Together they are what lets a slot's **photo be replaced**: the
  new image is clipped by the mask and composited onto the frame, at any size, without
  knowing anything about the PSD (§19.12).

**Every visible text layer except the logotype becomes a slot.** Measured on the sample:
the poster's ``logo`` and ``会客厅`` are *pixel* layers, so they stay artwork; ``EP.23``,
the title, four avatar names and the footer are *type* layers, and each one now comes out of
the background and is drawn by :mod:`animatter.media.text_overlay` instead. That is what
makes 期号 / 当期标题 / 头像名 editable at all — a text layer left in the composite can never
be changed, because the composite is a PNG.

Cost, measured on ``samples/播客模板.psd`` (2560×1440, 58 layers):

    PSDImage.open            0.44 s
    composite (background)   4.7 s     (pixels are *identical* with and without
                                        ignore_preview on this file, so honouring
                                        the flag costs nothing)
    composite (overlay)      0.8 s
    resize to 1920×1080      0.16 s

≈ 6 s once per import, which is why it runs as a background job rather than inside
the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
from PIL import Image
from psd_tools import PSDImage

from animatter.core import textslot
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media.fonts import EPISODE_FONT, NAME_FONT, TITLE_FONT

log = get_logger(__name__)

#: Every artefact is emitted at this size. 1080p is the deliverable; compositing at
#: the PSD's own canvas (2560×1440 here) and downscaling once is both sharper and
#: simpler than asking psd-tools for a smaller pass.
TARGET_W: Final[int] = 1920
TARGET_H: Final[int] = 1080

#: A media box must be big enough to matter and roughly 16:9. A group covering less
#: than a fifth of the canvas is decoration, not the content area.
_MIN_BOX_AREA_RATIO: Final[float] = 0.20
_MIN_BOX_ASPECT: Final[float] = 1.2
_MAX_BOX_ASPECT: Final[float] = 2.4

#: How far towards grey and how much darker the "not talking" avatar goes. Both
#: simpler approaches were tried on the real file and both look broken: dimming the
#: whole group turns the white plate behind the avatar grey (a visible grey box),
#: and lowering the group's alpha makes that plate translucent so the poster shows
#: through it. Neither touches alpha here — only the photo's own pixels change.
_DIM_DESATURATE: Final[float] = 0.65
_DIM_BRIGHTNESS: Final[float] = 0.52

#: A plate is a child that covers (almost) its group. The sample file's plate is a
#: full-bleed white ``pixel`` layer exactly the size of the group.
_PLATE_COVERAGE: Final[float] = 0.95


class _Layer(Protocol):
    """The slice of ``psd_tools`` layer API this module relies on."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def visible(self) -> bool: ...

    @property
    def bbox(self) -> tuple[int, int, int, int]: ...

    def is_group(self) -> bool: ...

    def __iter__(self) -> Any: ...


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


@dataclass(frozen=True)
class AvatarSlot:
    """One avatar position, with the pieces the renderer needs separately.

    ``frame`` + ``mask`` are the pair that makes the photo replaceable: the frame is the ring
    and plate with *no* photo and *no* name, and the mask says which pixels belonged to the
    photo. A replacement image is cover-fitted into ``mask_rect``, clipped by the mask and
    composited onto the frame — the result is the same picture the PSD shows, with a
    different face. Without the split, "change the avatar" would mean shipping a new PSD.
    """

    name: str
    rect: Rect
    #: The photo's box **relative to** ``rect``. Kept for the dim pass and for the default
    #: render; the authoritative shape is ``mask_rect``, which is the *visible* photo after
    #: the ring clips it.
    photo: Rect
    #: The name as it appears in the PSD. Only the **default text** of the name slot now —
    #: it is no longer baked into any PNG, so the user can rewrite it.
    label: str
    lit_path: str = ""
    dim_path: str = ""
    #: Ring + plate, no photo, no name. What a replacement image is composited onto.
    frame_path: str = ""
    #: One-channel PNG: 255 where the PSD's photo showed through the ring.
    mask_path: str = ""
    #: The mask's bounding box **relative to** ``rect`` — where a replacement is fitted.
    mask_rect: Rect = Rect(0, 0, 0, 0)
    #: The avatar's name text slot key (``avatar:<group>``), when it has one.
    name_key: str = ""


@dataclass(frozen=True)
class TextSlot:
    """One piece of text we draw, with everything needed to draw it again.

    ``key`` is the stable address the user's overrides are stored under. It is derived from
    the role and the PSD's own names, never from an index, so a recompile cannot silently
    move an edit onto a different piece of text.
    """

    key: str
    role: str
    label: str
    rect: Rect
    text: str
    #: Effective pixels at the compiled canvas size.
    font_size: int
    colour: str
    font: str
    name: str = ""


@dataclass
class CompiledTemplate:
    canvas_w: int
    canvas_h: int
    background_path: Path
    background_bare_path: Path
    overlay_path: Path
    box_rect: Rect
    avatars: list[AvatarSlot] = field(default_factory=list)
    texts: list[TextSlot] = field(default_factory=list)
    #: Worth telling the user on the confirm screen, e.g. "3 avatar groups were
    #: hidden in the PSD and are not part of the layout".
    notes: list[str] = field(default_factory=list)

    def slots_json(self) -> dict[str, Any]:
        return {
            "avatars": [
                {
                    "name": slot.name,
                    "rect": slot.rect.as_dict(),
                    "photo": slot.photo.as_dict(),
                    "label": slot.label,
                    "lit_path": slot.lit_path,
                    "dim_path": slot.dim_path,
                    "frame_path": slot.frame_path,
                    "mask_path": slot.mask_path,
                    "mask_rect": slot.mask_rect.as_dict(),
                    "name_key": slot.name_key,
                }
                for slot in self.avatars
            ],
            "texts": [
                {
                    "key": slot.key,
                    "role": slot.role,
                    "label": slot.label,
                    "name": slot.name,
                    "rect": slot.rect.as_dict(),
                    "text": slot.text,
                    "font_size": slot.font_size,
                    "colour": slot.colour,
                    "font": slot.font,
                }
                for slot in self.texts
            ],
            "notes": list(self.notes),
        }


# --- geometry -----------------------------------------------------------


def bbox_to_rect(bbox: tuple[int, int, int, int] | None) -> Rect | None:
    if bbox is None:
        return None
    left, top, right, bottom = (int(value) for value in bbox)
    if right <= left or bottom <= top:
        return None
    return Rect(x=left, y=top, w=right - left, h=bottom - top)


def scale_rect(rect: Rect, factor: float) -> Rect:
    """Scale both edges, then derive width from them.

    Rounding ``x`` and ``w`` independently lets ``x + w`` drift a pixel away from the
    scaled right edge, and that pixel shows up as a thin sliver of poster along the
    box border — invisible in review, obvious on a 1080p screen.
    """
    x = int(rect.x * factor + 0.5)
    y = int(rect.y * factor + 0.5)
    right = int(rect.right * factor + 0.5)
    bottom = int(rect.bottom * factor + 0.5)
    return Rect(x=x, y=y, w=max(1, right - x), h=max(1, bottom - y))


def even_rect(rect: Rect) -> Rect:
    """Grow to even width/height, keeping the top-left corner.

    ffmpeg needs even dimensions for ``yuv420p``; one odd pixel would make every
    encode fail at the very end with an unhelpful "width not divisible by 2".
    """
    return Rect(x=rect.x, y=rect.y, w=rect.w + rect.w % 2, h=rect.h + rect.h % 2)


def _aspect(rect: Rect) -> float:
    return rect.w / rect.h if rect.h else 0.0


# --- layer classification ----------------------------------------------


def layer_name(layer: Any) -> str:
    return str(getattr(layer, "name", "") or "")


def find_media_group(layers: list[Any], *, canvas_w: int, canvas_h: int) -> tuple[Any, Rect]:
    """The group holding the content plate.

    Found by geometry, not by layer name: 「图片PPT模板」 is one designer's wording in
    one language, and hard-coding it would make the feature work on exactly one
    file. The rule is "the largest visible group that is roughly 16:9 and covers a
    fifth of the canvas".
    """
    canvas_area = max(1, canvas_w * canvas_h)
    best: tuple[Any, Rect] | None = None
    best_area = 0
    for layer in layers:
        if not layer.is_group() or not layer.visible:
            continue
        rect = bbox_to_rect(layer.bbox)
        if rect is None:
            continue
        area = rect.w * rect.h
        if area / canvas_area < _MIN_BOX_AREA_RATIO:
            continue
        if not (_MIN_BOX_ASPECT <= _aspect(rect) <= _MAX_BOX_ASPECT):
            continue
        if area > best_area:
            best, best_area = (layer, rect), area

    if best is None:
        raise AppError(
            "template_no_media_box",
            "在这个 PSD 里找不到素材框",
            hint=(
                "素材框应该是一个可见的图层组，占画布两成以上、接近 16:9。"
                "确认 PSD 里有这样一个组，并且它是显示的。"
            ),
        )
    return best


def split_plate_and_content(group: Any, group_rect: Rect) -> tuple[Any | None, list[Any]]:
    """Split the media group into its plate and its content.

    The plate is the child covering (almost) the whole group — on the sample file a
    full-bleed white ``pixel`` layer exactly ``1920×1080`` at ``(320, 278)``.
    Everything else in the group is content and is removed from the background, so a
    dropped image has somewhere to land.
    """
    plate: Any | None = None
    content: list[Any] = []
    group_area = max(1, group_rect.w * group_rect.h)
    for child in group:
        rect = bbox_to_rect(child.bbox)
        if plate is None and rect is not None and rect.w * rect.h / group_area >= _PLATE_COVERAGE:
            plate = child
            continue
        content.append(child)
    return plate, content


def find_avatar_groups(layers: list[Any], media_group: Any) -> list[Any]:
    """Groups that hold one photo, so they are avatar positions.

    Matching on "has a smart-object child" rather than on a name prefix keeps a
    translated or reworded template working. Hidden groups are excluded: the sample
    file ships four extra avatar groups switched off, and treating them as live
    slots would draw four empty circles in the corner.
    """
    found: list[Any] = []
    for layer in layers:
        if not layer.is_group() or layer is media_group or not layer.visible:
            continue
        if bbox_to_rect(layer.bbox) is None:
            continue
        if any(getattr(child, "kind", "") == "smartobject" for child in layer):
            found.append(layer)
    return found


def text_layers_anywhere(layers: list[Any]) -> list[Any]:
    """Every visible type layer, including ones nested inside groups.

    ``find_text_layers`` only looked at the top level because P1 only needed to *report* the
    text it found. Now every one of them has to come out of the background — the four avatar
    names live inside avatar groups, and leaving them in would give the user two names for
    each guest: the one they can edit and the baked one underneath.
    """
    found: list[Any] = []
    stack: list[Any] = list(layers)
    while stack:
        layer = stack.pop()
        if not layer.visible:
            continue
        if layer.is_group():
            stack.extend(list(layer))
            continue
        if getattr(layer, "kind", "") == "type" and bbox_to_rect(layer.bbox) is not None:
            found.append(layer)
    return found


def avatar_of_text(layer: Any, avatar_groups: list[Any]) -> str | None:
    """Which avatar group this type layer belongs to, if any.

    Walking ``parent`` rather than comparing bboxes: a name plate may sit slightly *outside*
    its group's rect (the sample's ``嘉宾名`` is 3 px above it), so a geometric test would
    attribute it to nothing and leave it baked into the poster.
    """
    ids = {id(group): layer_name(group) for group in avatar_groups}
    parent = getattr(layer, "parent", None)
    while parent is not None:
        if id(parent) in ids:
            return ids[id(parent)]
        parent = getattr(parent, "parent", None)
    return None


def effective_scale(layer: Any) -> float:
    """The layer's transform scale (``xx`` of its affine matrix), 1.0 when absent.

    Photoshop bakes a scale into a text layer when it is resized with the transform tool
    instead of by editing the point size. On the sample, the avatar names carry 0.849 — so
    the PSD reports 25 px for a name that is drawn at 21.2 px, and using the raw number makes
    every guest's name a fifth too large.
    """
    transform = getattr(layer, "transform", None)
    if not transform:
        return 1.0
    try:
        value = float(transform[0])
    except (TypeError, ValueError, IndexError):
        return 1.0
    return value if 0.05 < value <= 8.0 else 1.0


def find_text_layers(layers: list[Any]) -> list[Any]:
    return [
        layer
        for layer in layers
        if getattr(layer, "kind", "") == "type"
        and layer.visible
        and not layer.is_group()
        and bbox_to_rect(layer.bbox) is not None
    ]


def make_background_filter(
    media_group: Any,
    content: list[Any],
    avatars: list[Any],
    overlay: list[Any],
    texts: list[Any] | None = None,
) -> Any:
    """What belongs in the poster: everything except box content, avatars, overlay, text.

    The overlay layers have to be excluded here as well as drawn on top. In the PSD
    the frame stroke sits *above* the media group, so removing it from the background
    and drawing it once above the box content is both correct and identical to what
    Photoshop shows. Leaving it in both places is not: a semi-transparent stroke
    composited twice is visibly darker along the border, and the content region
    stops being a clean plate.

    ``texts`` are excluded for a different reason: text that stays in the composite can
    never be edited, because the composite is a PNG. Every visible type layer is drawn by
    :mod:`animatter.media.text_overlay` on top instead — see §19.12.
    """
    content_ids = {id(child) for child in content}
    avatar_ids = {id(group) for group in avatars}
    overlay_ids = {id(layer) for layer in overlay}
    text_ids = {id(layer) for layer in (texts or [])}

    def keep(layer: Any) -> bool:
        if not layer.visible:
            return False
        ident = id(layer)
        if ident in avatar_ids or ident in content_ids or ident in overlay_ids:
            return False
        # Any type layer, wherever it sits — including the names inside avatar groups.
        if getattr(layer, "kind", "") == "type":
            return False
        if ident in text_ids:
            return False
        # A descendant of the media group that is not the plate is content, even
        # when nested deeper than the direct children checked above.
        parent = getattr(layer, "parent", None)
        while parent is not None:
            if id(parent) in content_ids:
                return False
            if parent is media_group:
                break
            parent = getattr(parent, "parent", None)
        return True

    return keep


def layers_above(layers: list[Any], target: Any) -> list[Any]:
    """Layers stacked above ``target``.

    ``psd-tools`` iterates top-first, so everything before ``target`` is drawn on top
    of it. Only those can need to be drawn above the box content; anything below is
    already part of the background.
    """
    result: list[Any] = []
    for layer in layers:
        if layer is target:
            break
        result.append(layer)
    return result


def _is_full_bleed(rect: Rect, *, canvas_w: int, canvas_h: int) -> bool:
    """A layer covering (nearly) the whole canvas is artwork, not a frame.

    The distinction matters because "stacked above the box" and "belongs above the
    box content" are not the same thing. The sample file's full-bleed texture sits
    above the media group but is part of the poster; drawing it as an overlay hides
    the poster entirely.
    """
    return rect.w * rect.h >= 0.7 * canvas_w * canvas_h


def avatar_photo_rect(group: Any, group_rect: Rect) -> Rect:
    """The photo's box **relative to** the group."""
    for child in group:
        if getattr(child, "kind", "") != "smartobject":
            continue
        rect = bbox_to_rect(child.bbox)
        if rect is not None:
            return Rect(
                x=rect.x - group_rect.x, y=rect.y - group_rect.y, w=rect.w, h=rect.h
            )
    return Rect(x=0, y=0, w=group_rect.w, h=group_rect.h)


# --- pixels -------------------------------------------------------------


def composite(psd: Any, layer_filter: Any) -> Image.Image:
    """Composite with ``ignore_preview=True`` explicitly.

    Without it, ``composite()`` short-circuits to the Photoshop thumbnail embedded in
    the file whenever one exists — byte-identical to ``topil()``. That sounds
    harmless and produces the worst kind of bug: the template changes and the
    background does not.
    """
    image: Image.Image = psd.composite(layer_filter=layer_filter, alpha=0.0, ignore_preview=True)
    return image.convert("RGBA")


def photo_mask(lit: Image.Image, plate: Image.Image) -> np.ndarray:
    """Which pixels are the **photo's**, as opposed to what sits behind it.

    The photo's bounding rect is not the photo: the group's own white plate lies under it,
    and on the sample file the plate is a full-bleed white ``pixel`` layer exactly the size
    of the group. Dimming the rect therefore turns that plate grey and the avatar comes out
    with a grey square behind its circle — the exact failure §19.9 lists first.

    The mask is computed rather than stored: composite the group once with the smart object
    and once without, and every pixel that changed belongs to the photo. Measured on the
    sample: 32,338 of 58,764 pixels, bbox 17,29–218,230 — inside the photo rect and
    excluding the plate corners.
    """
    a = np.array(lit.convert("RGBA")).astype(int)
    b = np.array(plate.convert("RGBA")).astype(int)
    # **Alpha counts, not just colour.** The photo's own line art is white, and the plate
    # composite has white RGB under a transparent alpha there — so a colour-only comparison
    # reads those pixels as "unchanged" and leaves a white speckle along every outline
    # (measured on the sample: the whole character came out stippled with white).
    colour_delta = np.abs(a[:, :, :3] - b[:, :, :3]).sum(axis=2)
    # A tolerance, not equality: anti-aliased edges blend the photo with the plate, and a
    # strict comparison would leave a one-pixel bright rim around the circle.
    mask: np.ndarray = (colour_delta > 6) | (np.abs(a[:, :, 3] - b[:, :, 3]) > 6)
    return mask


def dim_photo(
    image: Image.Image, photo: Rect, *, mask: np.ndarray | None = None
) -> Image.Image:
    """Desaturate and darken the photo's own pixels, leaving alpha untouched.

    ``mask`` is what keeps the plate out (see :func:`photo_mask`). Without one this falls
    back to the photo's rect, which is what P1 did and which produces the grey square.
    """
    array = np.array(image).astype(np.float32)
    left = max(0, photo.x)
    top = max(0, photo.y)
    right = min(array.shape[1], photo.right)
    bottom = min(array.shape[0], photo.bottom)
    if right <= left or bottom <= top:
        return image

    region = array[top:bottom, left:right, :3]
    grey = region.mean(axis=2, keepdims=True)
    dimmed = np.clip(
        (region * (1.0 - _DIM_DESATURATE) + grey * _DIM_DESATURATE) * _DIM_BRIGHTNESS, 0, 255
    )
    if mask is None:
        array[top:bottom, left:right, :3] = dimmed
        return Image.fromarray(array.astype(np.uint8), "RGBA")

    original = array[top:bottom, left:right, :3]
    patch = np.where(mask[top:bottom, left:right, None], dimmed, original)
    array[top:bottom, left:right, :3] = patch
    return Image.fromarray(array.astype(np.uint8), "RGBA")


def photo_mask_of(frame: Image.Image, with_photo: Image.Image) -> np.ndarray:
    """Which pixels belong to the photo, given the same group composited both ways.

    Computed rather than read from the PSD: the photo's *rect* is not the photo. On the
    sample, the smart object's bbox is 213×213 but the ring clips it to a 202×202 circle, so
    treating the rect as the shape leaves a square of face outside the circle.

    Alpha counts as well as colour, for the same reason ``photo_mask`` says: the photo's line
    art is white and the frame's own pixels are white too, so a colour-only comparison reads
    those as unchanged and leaves a speckled outline.
    """
    a = np.array(with_photo.convert("RGBA")).astype(int)
    b = np.array(frame.convert("RGBA")).astype(int)
    colour_delta = np.abs(a[:, :, :3] - b[:, :, :3]).sum(axis=2)
    mask: np.ndarray = (colour_delta > 6) | (np.abs(a[:, :, 3] - b[:, :, 3]) > 6)
    return mask


def mask_bbox(mask: np.ndarray) -> Rect | None:
    """The bounding box of a boolean mask, or ``None`` when it is empty."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    left, right = int(xs.min()), int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max()) + 1
    return Rect(x=left, y=top, w=right - left, h=bottom - top)


def mask_png(mask: np.ndarray) -> Image.Image:
    """An 8-bit mask as a greyscale PNG (255 = the photo's pixels)."""
    return Image.fromarray((mask.astype(np.uint8) * 255), "L")


def resize(image: Image.Image, width: int, height: int) -> Image.Image:
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _style_of(layer: Any) -> Any:
    """The first style run's data, or ``None``.

    ``psd_tools`` parses engine data into its own ``Dict`` type, which is **not** a ``dict``
    subclass. Any ``isinstance(x, dict)`` guard around these values silently fails and the
    caller gets the default — which is how every text slot came out black while the PSD was
    reporting real colours. Use mapping access only, never a type check.
    """
    try:
        return layer.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]
    except (AttributeError, KeyError, IndexError, TypeError):
        return None


def _colour_of(layer: Any) -> str:
    """The layer's fill colour as ``#rrggbb`` (see ``textslot.colour_from_values``).

    The values are ``[alpha, red, green, blue]`` — verified against the composite rather
    than trusted from the docs: ``EP.23``'s ``[1.0, 0.098, 0.278, 0.729]`` renders as
    ``#1947ba`` in the PSD, which is red=0.098, green=0.278, blue=0.729.
    """
    sheet = _style_of(layer)
    if sheet is None:
        return "#000000"
    fill = sheet.get("FillColor")
    if not fill:
        return "#000000"
    values = fill.get("Values")
    if not values:
        return "#000000"
    return textslot.colour_from_values(list(values))


def _run_count_of(layer: Any) -> int:
    """How many style runs the layer's text has.

    The sample's title has three: 「《罗小黑战记2》」blue, 「独家专访」red, 「六年磨一剑背后的
    苦与乐」mixed again. We draw **one** colour per slot — the first run's — because the
    override model has to answer "which run does the user's colour apply to?", and "the whole
    slot" is the only answer that stays sane when the text itself is rewritten. The count is
    reported so the UI can say so instead of the user noticing the red went blue.
    """
    try:
        runs = layer.engine_dict["StyleRun"]["RunArray"]
    except (AttributeError, KeyError, TypeError):
        return 0
    return len(list(runs)) if runs else 0


def _font_size_of(layer: Any) -> float:
    sheet = _style_of(layer)
    if sheet is None:
        return 0.0
    try:
        return float(sheet.get("FontSize") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _text_of(layer: Any) -> str:
    """The layer's default text, one line per paragraph.

    Photoshop stores a paragraph break as ``\r``, and the sample's title uses ``\r\r``
    between speakers (a blank line). Collapsing those would run three speakers together.
    """
    raw = str(getattr(layer, "text", "") or "")
    lines = [line.strip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


#: Which family each role starts with. The design calls for 数黑 on the title and the avatar
#: names, Baigo on the episode number — 数黑 is a display face and Baigo is a condensed
#: numeral face, and using one for both made the number look like part of the title.
_DEFAULT_FONT: dict[str, str] = {
    textslot.ROLE_TITLE: TITLE_FONT,
    textslot.ROLE_EPISODE: EPISODE_FONT,
    textslot.ROLE_AVATAR_NAME: NAME_FONT,
    textslot.ROLE_NOTE: TITLE_FONT,
}


def _text_slots(
    text_layers: list[Any],
    avatar_groups: list[Any],
    *,
    factor: float,
    canvas_w: int,
    canvas_h: int,
    content: textslot.Box | None = None,
) -> list[TextSlot]:
    """Classify the type layers and reduce them to drawable slots."""
    candidates: list[textslot.Candidate] = []
    for layer in text_layers:
        rect = bbox_to_rect(layer.bbox)
        if rect is None:
            continue
        candidates.append(
            textslot.Candidate(
                name=layer_name(layer),
                text=_text_of(layer),
                box=textslot.Box(x=rect.x, y=rect.y, w=rect.w, h=rect.h),
                font_size=_font_size_of(layer) * effective_scale(layer),
                colour=_colour_of(layer),
                avatar=avatar_of_text(layer, avatar_groups),
            )
        )

    # ``classify`` needs the PSD's own canvas to decide what counts as the header band;
    # the compiled ``factor`` is applied afterwards, to the boxes and the point sizes.
    slots = textslot.classify(
        candidates, canvas_w=canvas_w, canvas_h=canvas_h, content=content
    )

    out: list[TextSlot] = []
    for slot in slots:
        out.append(
            TextSlot(
                key=slot.key,
                role=slot.role,
                label=slot.label,
                rect=scale_rect(
                    Rect(x=slot.box.x, y=slot.box.y, w=slot.box.w, h=slot.box.h), factor
                ),
                text=slot.text,
                # ``slot.font_size`` is already the *effective* PSD size (point size × the
                # layer's transform scale), so only the canvas factor is left to apply.
                font_size=textslot.scaled_size(slot.font_size, 1.0, factor),
                colour=slot.colour,
                font=_DEFAULT_FONT.get(slot.role, TITLE_FONT),
                name=next(
                    (item.name for item in candidates if item.text == slot.text), ""
                ),
            )
        )
    return out


# --- the compile --------------------------------------------------------


def compile_template(psd_path: Path, out_dir: Path) -> CompiledTemplate:
    """Compile one PSD into the artefacts the renderer needs."""
    if not psd_path.is_file():
        raise AppError(
            "template_file_missing",
            f"找不到模板文件 {psd_path.name}",
            hint="重新导入这份 PSD。",
            status=404,
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        psd = PSDImage.open(str(psd_path))
    except Exception as exc:  # any parse failure is a user-facing one
        raise AppError(
            "template_unreadable",
            "这个文件打不开，可能不是有效的 PSD",
            hint=f"psd-tools 报错：{str(exc)[:200]}。用 Photoshop 另存一份再试。",
        ) from exc

    if psd.width <= 0 or psd.height <= 0:
        raise AppError("template_empty_canvas", "PSD 画布尺寸无效")

    if abs(psd.width / psd.height - TARGET_W / TARGET_H) > 0.02:
        raise AppError(
            "template_aspect",
            f"模板画布是 {psd.width}×{psd.height}，不是 16:9",
            hint="导出一个 16:9 的 PSD（例如 2560×1440 或 1920×1080）再导入。",
        )

    layers = list(psd)
    media_group, media_rect = find_media_group(layers, canvas_w=psd.width, canvas_h=psd.height)
    plate, content = split_plate_and_content(media_group, media_rect)
    avatar_groups = find_avatar_groups(layers, media_group)
    # Every visible type layer, nested ones included — the avatar names live inside their
    # groups, and a name left in the composite can never be edited (§19.12).
    text_layers = text_layers_anywhere(layers)

    notes: list[str] = []
    hidden_avatars = [
        layer_name(layer)
        for layer in layers
        if layer.is_group() and layer_name(layer).startswith("头像") and not layer.visible
    ]
    if hidden_avatars:
        notes.append(f"PSD 里有 {len(hidden_avatars)} 个隐藏的头像组，未计入版式")
    if plate is None:
        notes.append("素材框里没有铺满整组的底板，框内内容会出现透明")

    factor = TARGET_W / psd.width
    log.info(
        "template.scan",
        media_group=layer_name(media_group),
        media_rect=media_rect.as_dict(),
        plate=None if plate is None else layer_name(plate),
        content=[layer_name(child) for child in content],
        avatars=len(avatar_groups),
        texts=len(text_layers),
    )

    # The overlay has to be decided *before* the background is composited: it is
    # excluded from the background and drawn once above the box content instead.
    #
    # Full-bleed layers are deliberately **not** overlay. The sample file has
    # 「透明框架」, a 2560×1440 texture stacked above the media group; treating it as
    # an overlay drew a near-opaque white sheet over the entire poster (measured:
    # 44 % of the overlay's pixels carried alpha, and the composite lost the logo,
    # the title and every avatar). A layer that inks well outside the box is
    # artwork, not part of the frame around the box, so it belongs in the
    # background even though it sits higher in the PSD's stack.
    overlay_layers = [
        layer
        for layer in layers_above(layers, media_group)
        if layer.visible
        and (rect := bbox_to_rect(layer.bbox)) is not None
        and rect.intersects(media_rect)
        and not _is_full_bleed(rect, canvas_w=psd.width, canvas_h=psd.height)
    ]
    overlay_ids = {id(layer) for layer in overlay_layers}
    bare_path = out_dir / "background_bare.png"
    resize(
        composite(
            psd,
            make_background_filter(
                media_group, content, avatar_groups, overlay_layers, text_layers
            ),
        ),
        TARGET_W,
        TARGET_H,
    ).save(bare_path)

    overlay_path = out_dir / "overlay.png"
    resize(
        composite(psd, lambda layer: layer.visible and id(layer) in overlay_ids), TARGET_W, TARGET_H
    ).save(overlay_path)
    if overlay_layers:
        log.info("template.overlay", layers=[layer_name(layer) for layer in overlay_layers])

    # --- avatars ---------------------------------------------------------
    avatars: list[AvatarSlot] = []
    flat = Image.open(bare_path).convert("RGBA")
    for index, group in enumerate(avatar_groups):
        group_rect = bbox_to_rect(group.bbox)
        if group_rect is None:
            continue
        # Name-free: the name is a text slot now, so keeping it in the PNG would give the
        # user two names for every guest — the editable one and the baked one underneath.
        lit = group.composite(
            layer_filter=lambda layer: layer.kind != "type", alpha=0.0
        ).convert("RGBA")
        lit_path = out_dir / f"avatar{index}_lit.png"
        dim_path = out_dir / f"avatar{index}_dim.png"
        lit.save(lit_path)
        photo_rect = avatar_photo_rect(group, group_rect)

        # The replaceable-photo pair (§19.12): the ring/plate without any photo, plus the
        # mask saying which pixels the photo covered. A new image is cover-fitted into the
        # mask's bbox, clipped by the mask, composited onto the frame — so swapping a face
        # needs no PSD, no Photoshop and no knowledge of the template.
        frame_img = group.composite(
            layer_filter=lambda layer: layer.kind not in ("type", "smartobject"), alpha=0.0
        ).convert("RGBA")
        frame_path = out_dir / f"avatar{index}_frame.png"
        frame_path.write_bytes(b"")
        frame_img.save(frame_path)
        covered = photo_mask_of(frame_img, lit)
        clipped = mask_bbox(covered)
        mask_path = out_dir / f"avatar{index}_mask.png"
        mask_png(covered).save(mask_path)
        # The photo's own pixels, computed by compositing the group without it. Dimming the
        # photo's *rect* instead greys the white plate underneath it — a visible grey box
        # behind the circle, which is the first of the three dimming mistakes in §19.9.
        without_photo = group.composite(
            layer_filter=lambda layer: layer.kind != "smartobject", alpha=0.0
        ).convert("RGBA")
        dim_photo(lit, photo_rect, mask=photo_mask(lit, without_photo)).save(dim_path)

        target = scale_rect(group_rect, factor)
        label = next(
            (str(getattr(child, "text", "") or "") for child in group if child.kind == "type"),
            "",
        )
        avatars.append(
            AvatarSlot(
                name=layer_name(group),
                rect=target,
                photo=photo_rect,
                label=label,
                lit_path=str(lit_path),
                dim_path=str(dim_path),
                frame_path=str(frame_path),
                mask_path=str(mask_path),
                mask_rect=clipped or photo_rect,
                name_key=f"avatar:{layer_name(group)}",
            )
        )
        # Flatten it in for P1: it keeps the poster looking like the PSD until
        # per-speaker binding exists (P3 uses background_bare.png instead).
        flat.alpha_composite(resize(lit, target.w, target.h), dest=(target.x, target.y))

    background_path = out_dir / "background.png"
    flat.save(background_path)

    texts = _text_slots(
        text_layers,
        avatar_groups,
        factor=factor,
        canvas_w=psd.width,
        canvas_h=psd.height,
        content=textslot.Box(
            x=media_rect.x, y=media_rect.y, w=media_rect.w, h=media_rect.h
        ),
    )

    # Matched by layer name, not by position: ``_text_slots`` reorders (episode first, then
    # title, then names and notes by geometry) and ``text_layers`` is in PSD stack order.
    runs = {layer_name(layer): _run_count_of(layer) for layer in text_layers}
    multicolour = [slot.label for slot in texts if runs.get(slot.name, 0) > 1]
    if multicolour:
        notes.append(
            f"{'、'.join(multicolour)} 在 PSD 里有多段不同颜色，现在统一按第一段的颜色绘制"
        )

    if not any(slot.role == textslot.ROLE_TITLE for slot in texts):
        notes.append("没找到当期标题的文字层，标题需要在界面上手动输入")
    if not any(slot.role == textslot.ROLE_EPISODE for slot in texts):
        notes.append("没找到期号的文字层，期号需要在界面上手动输入")

    box = even_rect(scale_rect(media_rect, factor))
    if (box.w, box.h) != (scale_rect(media_rect, factor).w, scale_rect(media_rect, factor).h):
        notes.append(f"素材框尺寸已取偶到 {box.w}×{box.h}")

    return CompiledTemplate(
        canvas_w=TARGET_W,
        canvas_h=TARGET_H,
        background_path=background_path,
        background_bare_path=bare_path,
        overlay_path=overlay_path,
        box_rect=box,
        avatars=avatars,
        texts=texts,
        notes=notes,
    )
