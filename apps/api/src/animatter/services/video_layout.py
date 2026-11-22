"""The user's layout overrides, applied to a compiled template (§19.38).

What this service decides that :mod:`animatter.core.layout` cannot
------------------------------------------------------------------
The core module does the arithmetic. This one answers the three questions that need to know
about a *template*:

* **Which identifiers are real?** A payload may name a text slot that no longer exists or an
  avatar group that was never in the PSD. Unknown keys are refused on write (a user drag needs
  a slot on screen) and dropped on read (a recompile may legitimately remove one).
* **What does the renderer see?** :func:`box_of`, :func:`art_of`, :func:`draw_slots` and
  :func:`avatar_slots` are the four places the layout becomes pixels, and the preview, the still
  and the export all go through them.
* **Where does it live?** ``edits_json["layout"]`` — the same blob as the text and photo
  overrides, because a layout is an edit like any other: it belongs to the *project*, it is not
  part of the compiled artefact, and recompiling must not erase it.

That last point has a sharp edge: three writers share one JSON column, and each used to rebuild
the blob from the fields it knew about. :func:`animatter.services.video_edits.edits_of` now
carries a ``layout`` key through untouched, so a text edit cannot silently drop the arrangement
and a layout save cannot drop the title.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from animatter.core import layout as layout_core
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import VideoTemplate
from animatter.services import video_edits
from animatter.services import video_templates as template_service
from animatter.services.video_templates import Artwork

log = get_logger(__name__)


def canvas_of(row: VideoTemplate) -> tuple[int, int]:
    return row.canvas_w or 1920, row.canvas_h or 1080


def draggable_text_keys(row: VideoTemplate) -> set[str]:
    """Text slots the canvas editor may move.

    Everything except ``role == "content"``: those are the designer's placeholders inside the
    media box, they never render (``video_edits.draw_slots`` skips them), and offering a handle
    for text the film does not show is a promise the renderer has no intention of keeping. A
    stored rect for one is dropped on read as well, so a payload written before this rule
    cannot make the editor and the renderer disagree.
    """
    return {
        str(slot.get("key") or "")
        for slot in video_edits.slot_defaults(row)
        if str(slot.get("role") or "") != "content"
    }


def layout_of(row: VideoTemplate) -> layout_core.Layout:
    """The stored overrides, clamped to this canvas and stripped of stale keys.

    Clamped on read as well as on write because ``canvas_w`` can change under a stored rect (a
    recompile of a different PSD). A title placed at x=1500 on a 1920 wide poster is off-screen
    on a 1280 one, and off-screen text is invisible with no error.
    """
    width, height = canvas_of(row)
    parsed = layout_core.parse(video_edits.edits_of(row).get("layout"))
    known_texts = draggable_text_keys(row)
    known_avatars = {
        str(slot.get("name") or "") for slot in template_service.slot_list(row)
    }
    pruned = layout_core.Layout(
        box=parsed.box,
        texts={key: item for key, item in parsed.texts.items() if key in known_texts},
        avatars={key: item for key, item in parsed.avatars.items() if key in known_avatars},
    )
    return layout_core.sanitise(pruned, canvas_w=width, canvas_h=height)


def box_of(row: VideoTemplate) -> dict[str, int]:
    """The media box the film uses, in canvas pixels."""
    return layout_core.effective_box(template_service.box_rect_of(row), layout_of(row))


def art_of(row: VideoTemplate) -> tuple[Artwork | None, Artwork | None]:
    """``(plate, stroke)`` at their **current** rects.

    A no-op layout returns the compiled rects unchanged, so an untouched project keeps the same
    pixels — ``scale_art`` is exact when the box did not move.
    """
    plate, stroke = template_service.art_of(row)
    layout = layout_of(row)
    if layout.box is None:
        return plate, stroke
    compiled = template_service.box_rect_of(row)
    moved = layout_core.effective_box(compiled, layout)
    out: list[Artwork | None] = []
    for art in (plate, stroke):
        if art is None:
            out.append(None)
            continue
        rect = layout_core.scale_art(
            art.as_rect(), compiled_box=compiled, new_box=moved
        )
        out.append(
            Artwork(
                path=art.path,
                x=rect["x"],
                y=rect["y"],
                width=rect["w"],
                height=rect["h"],
            )
        )
    return out[0], out[1]


def art_signature(row: VideoTemplate) -> str:
    """The artwork geometry as a cache key, with the layout folded in.

    ``template_service.art_signature`` reads the *compiled* rects. This one reads where they will
    actually be drawn — the difference between a moved box invalidating the bake and a stale
    plate being reused.
    """
    plate, stroke = art_of(row)
    parts = [item.signature() if item is not None else "-" for item in (plate, stroke)]
    return "|".join(parts)


def avatar_rect(row: VideoTemplate, name: str, compiled: dict[str, int]) -> dict[str, int]:
    moved = layout_of(row).avatars.get(name)
    return moved.as_dict() if moved is not None else dict(compiled)


def avatar_slots(row: VideoTemplate) -> list[dict[str, Any]]:
    """Every avatar slot with its **current** rect, in ``slots_json`` order.

    The avatar track, the still preview, the plan and the panel's own geometry all read this, so
    a dragged frame cannot be right in one of them and wrong in another.
    """
    out: list[dict[str, Any]] = []
    for slot in template_service.slot_list(row):
        merged = dict(slot)
        rect = merged.get("rect")
        name = str(merged.get("name") or "")
        if isinstance(rect, dict) and name:
            merged["rect"] = avatar_rect(row, name, rect)
        out.append(merged)
    return out


def apply_to_slots(
    row: VideoTemplate, slots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The resolved text slots with the user's rects (and scaled type sizes) applied.

    Called by :func:`animatter.services.video_edits.resolved_texts`, which is the single source
    of truth for "what text the film shows" — so every consumer (panel, still, export) sees the
    moved rect, and 「拖了框但画面还在原地」 cannot happen in one of them only.

    Font size: when the user resizes a text box, the type scales by the smaller of the two
    factors (decision ⑬ — a box dragged wider but not taller must not overflow its own height).
    An explicit ``font_size`` in the stored override wins, because that is the user typing a
    number into the text panel and expecting exactly that number.
    """
    compiled_by_key = {
        str(item.get("key") or ""): item for item in video_edits.slot_defaults(row)
    }
    compiled_avatars = {
        str(item.get("name") or ""): item for item in template_service.slot_list(row)
    }
    moved = layout_of(row)
    out: list[dict[str, Any]] = []
    for slot in slots:
        key = str(slot.get("key") or "")
        merged = dict(slot)
        compiled = compiled_by_key.get(key)
        placement = moved.texts.get(key)
        if compiled is None:
            out.append(merged)
            continue
        compiled_rect = compiled.get("rect") or {}
        if not isinstance(compiled_rect, dict):
            compiled_rect = {}
        if slot.get("role") == "avatar_name":
            group = key.removeprefix("avatar:")
            frame = compiled_avatars.get(group) or {}
            frame_rect = frame.get("rect")
            rect = layout_core.cascade_name_plate(
                compiled_rect,
                compiled_avatar=frame_rect if isinstance(frame_rect, dict) else {},
                moved=moved.avatars.get(group),
            )
            merged["rect"] = rect
            out.append(merged)
            continue
        if placement is None:
            out.append(merged)
            continue
        merged["rect"] = {axis: getattr(placement, axis) for axis in ("x", "y", "w", "h")}
        if placement.font_size is not None:
            merged["font_size"] = placement.font_size
        elif compiled.get("font_size"):
            merged["font_size"] = layout_core.scale_font(
                int(compiled["font_size"]),
                compiled=compiled_rect,
                moved=placement,
            )
        out.append(merged)
    return out


def _require_canvas(payload: dict[str, Any], row: VideoTemplate) -> None:
    if row.status != "ready":
        raise AppError("template_not_ready", "模板还没编译好", status=409)


def save(
    session: Session,
    project_id: str,
    *,
    box: dict[str, int] | None = None,
    texts: dict[str, dict[str, int]] | None = None,
    avatars: dict[str, dict[str, int]] | None = None,
    replace: bool = False,
) -> VideoTemplate:
    """Store the arrangement. Missing keys mean "leave as compiled".

    ``replace`` writes the whole object instead of merging, which is what the editor needs when a
    slot is dragged *back* to where the compile put it — a merge cannot express "forget this
    override" without a separate delete, and two code paths for one user action is how they end
    up disagreeing.
    """
    row = template_service.require(session, project_id)
    _require_canvas({}, row)
    width, height = canvas_of(row)

    known_texts = draggable_text_keys(row)
    all_texts = {str(slot.get("key") or "") for slot in video_edits.slot_defaults(row)}
    known_avatars = {str(slot.get("name") or "") for slot in template_service.slot_list(row)}
    for key in (texts or {}):
        if key not in known_texts:
            if key in all_texts:
                raise AppError(
                    "layout_slot_fixed",
                    f"「{key}」是模板里固定的说明文字，不能移动",
                    hint="它在素材框里给作者看的，成片不画。",
                    status=409,
                )
            raise AppError(
                "layout_slot_unknown",
                f"模板里没有「{key}」这块文字",
                hint="可能是模板重新编译过，刷新页面再试。",
                status=404,
            )
    for key in (avatars or {}):
        if key not in known_avatars:
            raise AppError(
                "layout_slot_unknown",
                f"模板里没有「{key}」这个头像位",
                hint="可能是模板重新编译过，刷新页面再试。",
                status=404,
            )

    current = layout_of(row)
    texts_out = {} if replace else dict(current.texts)
    for key, raw in (texts or {}).items():
        parsed = layout_core.parse_placement(raw)
        if parsed is None:
            raise AppError("layout_rect_invalid", "这个位置的数据不完整", status=422)
        texts_out[key] = layout_core.clamp_placement(
            parsed,
            canvas_w=width,
            canvas_h=height,
            min_w=layout_core.MIN_TEXT_W,
            min_h=layout_core.MIN_TEXT_H,
        )
    avatars_out = {} if replace else dict(current.avatars)
    for key, raw in (avatars or {}).items():
        parsed = layout_core.parse_placement(raw)
        if parsed is None:
            raise AppError("layout_rect_invalid", "这个位置的数据不完整", status=422)
        avatars_out[key] = layout_core.clamp_placement(
            parsed,
            canvas_w=width,
            canvas_h=height,
            min_w=layout_core.MIN_AVATAR_W,
            min_h=layout_core.MIN_AVATAR_H,
        )
    box_out = None if replace else current.box
    if box is not None:
        parsed_box = layout_core.parse_placement(box)
        if parsed_box is None:
            raise AppError("layout_rect_invalid", "素材框的数据不完整", status=422)
        box_out = layout_core.clamp_placement(
            parsed_box,
            canvas_w=width,
            canvas_h=height,
            min_w=layout_core.MIN_BOX_W,
            min_h=layout_core.MIN_BOX_H,
            even=True,
        )

    merged = _drop_defaults(
        row, layout_core.Layout(box=box_out, texts=texts_out, avatars=avatars_out)
    )
    _write(row, merged)
    session.flush()
    log.info(
        "template.layout_saved",
        template=row.id,
        box=merged.box is not None,
        texts=len(merged.texts),
        avatars=len(merged.avatars),
    )
    return row


def reset(session: Session, project_id: str) -> VideoTemplate:
    """Back to the compiled layout — the only undo this editor has."""
    row = template_service.require(session, project_id)
    _write(row, layout_core.Layout())
    session.flush()
    log.info("template.layout_reset", template=row.id)
    return row


def _rect_of(slot: dict[str, Any]) -> dict[str, Any]:
    """The ``rect`` of one compiled slot, or an empty dict when it has none."""
    rect = slot.get("rect")
    return rect if isinstance(rect, dict) else {}


def _same(rect: dict[str, Any], compiled: dict[str, Any]) -> bool:
    """Whether two rects are the same placement, within a pixel of rounding."""
    if not compiled:
        return False
    return all(
        abs(int(rect.get(axis) or 0) - int(compiled.get(axis) or 0)) <= 1
        for axis in ("x", "y", "w", "h")
    )


def _drop_defaults(
    row: VideoTemplate, layout: layout_core.Layout
) -> layout_core.Layout:
    """Remove placements that put a slot exactly where the compile did.

    A drag that ends where it started, and a per-slot 「恢复默认」, both produce a rect equal to
    the compiled one. Storing it would be harmless to the picture and harmful to everything that
    reads the layout as a *difference*: ``is_empty`` would say 「已改动」 forever for a project
    nobody touched, and the four cache keys would all change for a save that changed nothing.
    """
    compiled_box = template_service.box_rect_of(row)
    box = layout.box
    if box is not None and _same(box.as_dict(), compiled_box):
        box = None
    compiled_texts = {
        str(slot.get("key") or ""): _rect_of(slot)
        for slot in video_edits.slot_defaults(row)
    }
    compiled_avatars = {
        str(slot.get("name") or ""): _rect_of(slot)
        for slot in template_service.slot_list(row)
    }
    texts = {
        key: item
        for key, item in layout.texts.items()
        if not _same(item.as_dict(), compiled_texts.get(key) or {})
    }
    avatars = {
        key: item
        for key, item in layout.avatars.items()
        if not _same(item.as_dict(), compiled_avatars.get(key) or {})
    }
    return layout_core.Layout(box=box, texts=texts, avatars=avatars)


def _write(row: VideoTemplate, layout: layout_core.Layout) -> None:
    """Put the layout back in ``edits_json`` **without touching** the text and photo overrides."""
    data = video_edits.edits_of(row)
    payload: dict[str, Any] = {"texts": data["texts"], "photos": data["photos"]}
    if not layout.is_empty():
        payload["layout"] = layout.as_json()
    row.edits_json = json.dumps(payload, ensure_ascii=False)


@dataclass(frozen=True)
class SlotBox:
    """One draggable thing on the canvas, as the editor needs it."""

    key: str
    kind: str
    label: str
    x: int
    y: int
    w: int
    h: int
    font_size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }
        if self.font_size is not None:
            out["font_size"] = self.font_size
        return out


def _asset_url(row: VideoTemplate, path: str) -> str:
    return template_service.asset_url(row.project_id, path, row.status)


def _avatar_lit(row: VideoTemplate, slot: dict[str, Any]) -> str:
    """The portrait the editor should draw for this slot — the **replacement** when there is one.

    ``slots_json`` records what the compile produced, so reading ``lit_path`` directly would show
    the PSD's own photo in the editor after the user replaced it. The film would show the new
    face and the canvas the old one; the canvas is the one people trust before pressing 导出.
    """
    replacement = video_edits.photo_paths(row, slot)
    path = replacement[0] if replacement is not None else slot.get("lit_path")
    return _asset_url(row, str(path or ""))


def editor_state(row: VideoTemplate) -> dict[str, Any]:
    """Everything the canvas editor draws, resolved to where it currently is.

    One payload instead of three round trips: the box, the artwork rects, every text slot and
    every avatar frame with **current** geometry plus the compiled geometry, so 「恢复默认」 on a
    single slot can be drawn without another request.
    """
    layout = layout_of(row)
    compiled_box = template_service.box_rect_of(row)
    plate, stroke = art_of(row)
    width, height = canvas_of(row)
    texts = video_edits.resolved_texts(row)
    avatars = avatar_slots(row)
    return {
        "canvas": {"w": width, "h": height},
        "box": box_of(row),
        "compiled_box": compiled_box,
        "plate": plate.as_rect() if plate is not None else None,
        "stroke": stroke.as_rect() if stroke is not None else None,
        # The four images the editor draws underneath its handles, so the canvas is the poster
        # the film will use rather than a grey rectangle (decision ⑱). Bunched together and
        # named by role, because a client that receives one URL and not the others has to be
        # able to tell which piece is missing.
        # The **open** poster (no plate, no avatars) is the film's own stack's bottom layer:
        # `background.png` has both baked in, so drawing the plate and the faces on top of it
        # would show each of them twice — at exactly the same place, which is the kind of double
        # image that reads as a rendering bug rather than as a compositing one.
        "background_url": _asset_url(row, str(template_service.poster_of(row, bare=True))),
        "plate_url": _asset_url(row, str(plate.path)) if plate is not None else "",
        "stroke_url": _asset_url(row, str(stroke.path)) if stroke is not None else "",
        "texts": [
            SlotBox(
                key=str(slot.get("key") or ""),
                kind="text",
                label=str(slot.get("label") or ""),
                x=int((slot.get("rect") or {}).get("x") or 0),
                y=int((slot.get("rect") or {}).get("y") or 0),
                w=int((slot.get("rect") or {}).get("w") or 0),
                h=int((slot.get("rect") or {}).get("h") or 0),
                font_size=int(slot["font_size"]) if slot.get("font_size") else None,
            ).as_dict()
            for slot in texts
            # The designer's placeholders never render, so they cannot be dragged either:
            # offering a handle for something the film does not show is a promise the render
            # has no intention of keeping.
            if str(slot.get("role")) != "content"
        ],
        "avatars": [
            SlotBox(
                key=str(slot.get("name") or ""),
                kind="avatar",
                label=str(slot.get("label_name") or slot.get("name") or ""),
                x=int((slot.get("rect") or {}).get("x") or 0),
                y=int((slot.get("rect") or {}).get("y") or 0),
                w=int((slot.get("rect") or {}).get("w") or 0),
                h=int((slot.get("rect") or {}).get("h") or 0),
            ).as_dict()
            | {"lit_url": _avatar_lit(row, slot)}
            for slot in avatars
        ],
        "changed": layout.as_json(),
        "is_default": layout.is_empty(),
    }
