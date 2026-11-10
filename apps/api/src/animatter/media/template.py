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
* slot geometry — the content rect, avatar slots (ring + photo + name), text slots.

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

from animatter.errors import AppError
from animatter.log import get_logger

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
    """One avatar position, with the pieces the renderer needs separately."""

    name: str
    rect: Rect
    #: The photo's box **relative to** ``rect``, so the dim pass can be applied to
    #: the small lit PNG without re-deriving anything.
    photo: Rect
    #: The name as it appears in the PSD. Read-only: it is flattened into the
    #: background as artwork (PSD text cannot be rewritten), and P3 draws the
    #: speaker's name itself.
    label: str
    lit_path: str = ""
    dim_path: str = ""


@dataclass(frozen=True)
class TextSlot:
    name: str
    rect: Rect
    text: str


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
                }
                for slot in self.avatars
            ],
            "texts": [
                {"name": slot.name, "rect": slot.rect.as_dict(), "text": slot.text}
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
    media_group: Any, content: list[Any], avatars: list[Any], overlay: list[Any]
) -> Any:
    """What belongs in the poster: everything except box content, avatars, overlay.

    The overlay layers have to be excluded here as well as drawn on top. In the PSD
    the frame stroke sits *above* the media group, so removing it from the background
    and drawing it once above the box content is both correct and identical to what
    Photoshop shows. Leaving it in both places is not: a semi-transparent stroke
    composited twice is visibly darker along the border, and the content region
    stops being a clean plate.
    """
    content_ids = {id(child) for child in content}
    avatar_ids = {id(group) for group in avatars}
    overlay_ids = {id(layer) for layer in overlay}

    def keep(layer: Any) -> bool:
        if not layer.visible:
            return False
        ident = id(layer)
        if ident in avatar_ids or ident in content_ids or ident in overlay_ids:
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


def dim_photo(image: Image.Image, photo: Rect) -> Image.Image:
    """Desaturate and darken the photo's own pixels, leaving alpha untouched."""
    array = np.array(image).astype(np.float32)
    left = max(0, photo.x)
    top = max(0, photo.y)
    right = min(array.shape[1], photo.right)
    bottom = min(array.shape[0], photo.bottom)
    if right <= left or bottom <= top:
        return image

    region = array[top:bottom, left:right, :3]
    grey = region.mean(axis=2, keepdims=True)
    dimmed = (region * (1.0 - _DIM_DESATURATE) + grey * _DIM_DESATURATE) * _DIM_BRIGHTNESS
    array[top:bottom, left:right, :3] = np.clip(dimmed, 0, 255)
    return Image.fromarray(array.astype(np.uint8), "RGBA")


def resize(image: Image.Image, width: int, height: int) -> Image.Image:
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


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
    text_layers = find_text_layers(layers)

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
        composite(psd, make_background_filter(media_group, content, avatar_groups, overlay_layers)),
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
        lit = group.composite(alpha=0.0).convert("RGBA")
        lit_path = out_dir / f"avatar{index}_lit.png"
        dim_path = out_dir / f"avatar{index}_dim.png"
        lit.save(lit_path)
        photo_rect = avatar_photo_rect(group, group_rect)
        dim_photo(lit, photo_rect).save(dim_path)

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
            )
        )
        # Flatten it in for P1: it keeps the poster looking like the PSD until
        # per-speaker binding exists (P3 uses background_bare.png instead).
        flat.alpha_composite(resize(lit, target.w, target.h), dest=(target.x, target.y))

    background_path = out_dir / "background.png"
    flat.save(background_path)

    texts = [
        TextSlot(
            name=layer_name(layer),
            rect=scale_rect(rect, factor),
            text=str(getattr(layer, "text", "") or "").replace("\r", "\n").strip(),
        )
        for layer in text_layers
        if (rect := bbox_to_rect(layer.bbox)) is not None
    ]

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
