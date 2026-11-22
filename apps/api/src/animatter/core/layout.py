"""Where things sit on the template canvas — the user's layout overrides (§19.38).

The compile decides where the poster's pieces are: the media box, each text slot, each avatar
frame. This module holds **only what the user moved afterwards**, keyed by the same identifiers
the compile used (``episode`` / ``title`` / ``avatar:<PSD group>``), so a recompile of the same
PSD keeps the user's arrangement and a different PSD does not inherit it.

Why overrides and not the new positions
---------------------------------------
Storing absolute rects for every slot would freeze the layout at the moment of the first drag:
a recompile that widens the title, or a default template that ships a better arrangement, could
never reach a project that had once moved one avatar. Keyed deltas have the same problem in a
milder form, and they make "restore default" mean *delete a key*. So: absent = the compile's
answer, present = the user's, and the default layout is the absence of this object.

Three derived placements
------------------------
Two things are not dragged independently, and this module is where that is decided once:

* **plate and stroke follow the box.** They are the artwork *of* the box, so they scale with
  it, around the box's own origin. Equal box in, equal rects out — byte-identical pixels, which
  is what makes the layout editor safe to ship (§19.35).
* **an avatar's name plate follows its frame.** The plate is a text slot 5 px inside the frame's
  top-left corner; dragging the frame and leaving the name behind puts 「彼方」 where nobody is.

Pure: no IO, no database, no knowledge of who calls it. The renderer, the plan and the panel all
call the same functions, which is the only way the preview and the film can agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: A box smaller than this cannot show footage, and the plate/stroke would be slivers.
MIN_BOX_W = 160
MIN_BOX_H = 90

#: Text slots can be small (an avatar name plate is 30 px wide), but zero-size is not a slot.
MIN_TEXT_W = 8
MIN_TEXT_H = 8

#: Avatar frames hold a 230-ish px portrait; below this they are a dot.
MIN_AVATAR_W = 24
MIN_AVATAR_H = 24


@dataclass(frozen=True)
class Placement:
    """One rectangle. ``font_size`` is only meaningful for text slots."""

    x: int
    y: int
    w: int
    h: int
    font_size: int | None = None

    def as_dict(self) -> dict[str, int]:
        out = {"x": self.x, "y": self.y, "w": self.w, "h": self.h}
        if self.font_size is not None:
            out["font_size"] = self.font_size
        return out

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def shifted(self, dx: int, dy: int) -> Placement:
        return Placement(
            x=self.x + dx, y=self.y + dy, w=self.w, h=self.h, font_size=self.font_size
        )


@dataclass(frozen=True)
class Layout:
    """The user's overrides. An empty Layout *is* the compiled layout."""

    box: Placement | None = None
    texts: dict[str, Placement] = field(default_factory=dict)
    avatars: dict[str, Placement] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return self.box is None and not self.texts and not self.avatars

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.box is not None:
            out["box"] = self.box.as_dict()
        if self.texts:
            out["texts"] = {key: item.as_dict() for key, item in self.texts.items()}
        if self.avatars:
            out["avatars"] = {key: item.as_dict() for key, item in self.avatars.items()}
        return out

    def signature(self) -> str:
        """A stable cache-key string; ``-`` when nothing was moved.

        Sorted keys so the same arrangement written in a different order is the same key — the
        four digests that use this (text PNG, baked plate, still, plan) would otherwise produce
        a new URL for a no-op save.
        """
        if self.is_empty():
            return "-"
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))


def _int(value: object, default: int = 0) -> int:
    """``int`` from JSON without raising: floats truncate, strings are parsed, junk defaults.

    JSON is user-touchable data (a hand-edited row, a payload from an older client). A layout
    that cannot be read must degrade to the compiled one — the same rule ``video_edits`` follows
    for text overrides — never fail an export that already spent minutes encoding.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    if isinstance(value, str):
        try:
            return round(float(value))
        except ValueError:
            return default
    return default


def parse_placement(raw: object) -> Placement | None:
    """One rect from JSON, or ``None`` when the object is not a rect at all."""
    if not isinstance(raw, dict):
        return None
    if any(axis not in raw for axis in ("x", "y", "w", "h")):
        return None
    size = raw.get("font_size")
    return Placement(
        x=_int(raw["x"]),
        y=_int(raw["y"]),
        w=_int(raw["w"]),
        h=_int(raw["h"]),
        font_size=_int(size) if size is not None else None,
    )


def parse(raw: object) -> Layout:
    """The stored layout, tolerating anything malformed.

    Accepts the JSON string (what ``edits_json`` holds) as well as the decoded object, so tests
    and service code do not have to agree about which layer decodes.
    """
    data: object = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            return Layout()
    if not isinstance(data, dict):
        return Layout()
    box = parse_placement(data.get("box"))
    texts_raw = data.get("texts")
    avatars_raw = data.get("avatars")
    texts = (
        {
            str(key): item
            for key, value in texts_raw.items()
            if (item := parse_placement(value)) is not None and str(key)
        }
        if isinstance(texts_raw, dict)
        else {}
    )
    avatars = (
        {
            str(key): item
            for key, value in avatars_raw.items()
            if (item := parse_placement(value)) is not None and str(key)
        }
        if isinstance(avatars_raw, dict)
        else {}
    )
    return Layout(box=box, texts=texts, avatars=avatars)


def _even(value: int) -> int:
    return value - (value % 2)


def clamp_placement(
    placement: Placement,
    *,
    canvas_w: int,
    canvas_h: int,
    min_w: int,
    min_h: int,
    even: bool = False,
) -> Placement:
    """Keep a rect on the canvas and no smaller than ``min_w``/``min_h``.

    Clamping rather than rejecting: the values arrive from a drag that already happened on the
    user's screen, and refusing it means the picture on their screen and the one in the file
    disagree. ``even`` is for the media box — libx264 with ``yuv420p`` needs even dimensions,
    and an odd box would fail the encode minutes later with a message about pixel formats.
    """
    width = max(min_w, min(_int(canvas_w, min_w), placement.w))
    height = max(min_h, min(_int(canvas_h, min_h), placement.h))
    if even:
        width = max(min_w, _even(width))
        height = max(min_h, _even(height))
    x = max(0, min(_int(canvas_w, width) - width, placement.x))
    y = max(0, min(_int(canvas_h, height) - height, placement.y))
    return Placement(x=x, y=y, w=width, h=height, font_size=placement.font_size)


def sanitise(layout: Layout, *, canvas_w: int, canvas_h: int) -> Layout:
    """Every override clamped to this canvas. Used on write *and* on read.

    On read as well, because ``canvas_w`` can change (a recompile of a different PSD) and a rect
    that was valid on 1920x1080 may put a title off the edge of a 1280x720 poster. Off-canvas
    text is invisible with no error, which is the worst failure shape here.
    """
    box = (
        clamp_placement(
            layout.box,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            min_w=MIN_BOX_W,
            min_h=MIN_BOX_H,
            even=True,
        )
        if layout.box is not None
        else None
    )
    texts = {
        key: clamp_placement(
            item,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            min_w=MIN_TEXT_W,
            min_h=MIN_TEXT_H,
        )
        for key, item in layout.texts.items()
    }
    avatars = {
        key: clamp_placement(
            item,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            min_w=MIN_AVATAR_W,
            min_h=MIN_AVATAR_H,
        )
        for key, item in layout.avatars.items()
    }
    return Layout(box=box, texts=texts, avatars=avatars)


def effective_box(compiled: dict[str, int], layout: Layout) -> dict[str, int]:
    """The box rectangle the film uses: the user's, or the compile's."""
    if layout.box is None:
        return dict(compiled)
    return layout.box.as_dict()


def scale_art(
    art: dict[str, int],
    *,
    compiled_box: dict[str, int],
    new_box: dict[str, int],
) -> dict[str, int]:
    """Move/scale one piece of box artwork with the box.

    Both the plate and the stroke are positioned *relative to the box*, so the transform is
    "the offset from the box origin scales with the box". With ``compiled_box == new_box`` every
    term is exact and the result is the input dict — which is what keeps an untouched project
    pixel-identical after this round.
    """
    old_w = int(compiled_box.get("w") or 0)
    old_h = int(compiled_box.get("h") or 0)
    if old_w <= 0 or old_h <= 0:
        return dict(art)
    sx = int(new_box.get("w") or old_w) / old_w
    sy = int(new_box.get("h") or old_h) / old_h
    dx = int(art.get("x") or 0) - int(compiled_box.get("x") or 0)
    dy = int(art.get("y") or 0) - int(compiled_box.get("y") or 0)
    return {
        "x": round(int(new_box.get("x") or 0) + dx * sx),
        "y": round(int(new_box.get("y") or 0) + dy * sy),
        "w": max(1, round(int(art.get("w") or 0) * sx)),
        "h": max(1, round(int(art.get("h") or 0) * sy)),
    }


def cascade_name_plate(
    plate: dict[str, int],
    *,
    compiled_avatar: dict[str, int],
    moved: Placement | None,
) -> dict[str, int]:
    """Shift a name plate by however far its avatar frame moved.

    The plate's own offset from the frame's corner is preserved (the compile puts it at +5,+2 and
    its exact width depends on the PSD). ``moved`` is ``None`` when the frame was not dragged, so
    the plate keeps its compiled rect — again identity by construction.
    """
    if moved is None:
        return dict(plate)
    dx = moved.x - int(compiled_avatar.get("x") or 0)
    dy = moved.y - int(compiled_avatar.get("y") or 0)
    if dx == 0 and dy == 0:
        return dict(plate)
    return {
        "x": int(plate.get("x") or 0) + dx,
        "y": int(plate.get("y") or 0) + dy,
        "w": int(plate.get("w") or 0),
        "h": int(plate.get("h") or 0),
    }


def scale_font(size: int, *, compiled: dict[str, int], moved: Placement) -> int:
    """Font size for a text box the user resized: scale by the smaller factor.

    ``min`` because a box dragged wide but not tall must not produce type that no longer fits
    on the line; ``_fit`` would then shrink it back and the text would look stuck at its old
    size, which is the complaint that motivated scaling in the first place.

    Snapped to even pixels for the same reason the box is: ``_fit`` steps by 2 px, so an odd
    request lands on the same size as its even neighbour and the UI would report a value the
    picture does not have.
    """
    if size <= 0:
        return 0
    old_w = max(1, int(compiled.get("w") or 0))
    old_h = max(1, int(compiled.get("h") or 0))
    factor = min(moved.w / old_w, moved.h / old_h)
    return max(2, _even(round(size * factor)))
