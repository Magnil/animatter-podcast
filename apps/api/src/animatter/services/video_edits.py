"""User edits on top of a compiled template: text overrides and avatar photos (§19.12).

What lives here
---------------
The compile produces *defaults* — the PSD's own title, episode number, avatar names and
photos. This service applies the user's changes to those defaults without touching the
compile output:

* **text** — ``edits_json["texts"][<slot key>]`` overrides text / font / colour / size. The
  slot's rect and its default size still come from the PSD, which is the whole point of
  "remember the layout, edit the content".
* **photos** — ``edits_json["photos"][<slot name>]`` names an imported asset. The replacement
  is composited by :mod:`animatter.media.text_overlay.fit_photo` into the slot's frame, and
  both the lit and dim PNGs are rewritten from it.

Why overrides are applied at *render* time and not baked at edit time
--------------------------------------------------------------------
Baking would mean every edit rewrites the compiled artefacts, so switching a title back to
the designer's text would have no way home, and a recompile would have to be forbidden. Kept
as data, the compile output stays a pure function of the PSD and the user's edits stay a pure
function of what they typed — each can be thrown away independently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core import textslot
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import fonts
from animatter.media import text_overlay as overlay_media
from animatter.models import VideoTemplate
from animatter.services import video_assets as asset_service
from animatter.services import video_templates as template_service

log = get_logger(__name__)

#: Where replacement avatar art is written. Under the template's own build directory so it
#: is deleted with the template, and named per slot so a second edit is a clean overwrite.
EDIT_SUBDIR = "edits"

#: Fonts the picker offers first. Not a whitelist — the endpoint returns every installed
#: family — but these two are the design and should not be 60 entries down a dropdown.
PREFERRED_FONTS = (fonts.TITLE_FONT, fonts.EPISODE_FONT, "Microsoft YaHei", "SimHei")


@dataclass(frozen=True)
class TextEdit:
    """What the user changed about one text slot. Every field is optional."""

    text: str | None = None
    font: str | None = None
    colour: str | None = None
    font_size: int | None = None


def _loads(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def edits_of(row: VideoTemplate) -> dict[str, Any]:
    """The stored edits, tolerating anything malformed.

    A hand-edited or half-written blob degrades to "no edits", which renders the PSD's own
    text — the same rule ``video_avatars.bindings_of`` follows, and for the same reason: a
    broken override must not fail an export that already spent minutes encoding.
    """
    data = _loads(row.edits_json)
    texts = data.get("texts")
    photos = data.get("photos")
    return {
        "texts": texts if isinstance(texts, dict) else {},
        "photos": photos if isinstance(photos, dict) else {},
        # The canvas arrangement (§19.38) shares this column with the other two. It is carried
        # through untouched rather than parsed here: a text edit that rebuilt the blob from the
        # fields it knew about used to drop everything else, and losing a saved layout silently
        # when someone retypes a title is exactly the shape of bug this repository keeps
        # re-learning.
        "layout": data.get("layout"),
    }


def slot_defaults(row: VideoTemplate) -> list[dict[str, Any]]:
    """Every compiled text slot, exactly as the PSD defined it."""
    slots = _loads(row.slots_json)
    texts = slots.get("texts")
    return [item for item in texts if isinstance(item, dict)] if isinstance(texts, list) else []


def resolved_texts(
    row: VideoTemplate, *, avatar_names: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The defaults with the user's edits layered on top.

    This is the single source of truth for "what text will the film show", used by both the
    still preview and the export. Two call sites computing it separately is how the preview
    and the film end up disagreeing about the title.

    ``avatar_names`` is what an avatar's name plate should say, keyed by the PSD group name —
    that is, **the name of the speaker bound to it** (round 99). The name plate used to be its
    own editable field, which meant the same 「主播1」 appeared in two places and the poster
    could show a name that did not belong to the person talking underneath. With the field
    gone the plate is fully derived: text from the bound speaker, font/size/colour from the
    PSD. An override stored under ``avatar:<group>`` is therefore **ignored**, not merely
    outranked — a value that nothing on the screen can edit but that still changes the
    picture is a ghost, and it would reappear the moment the slot was unbound.
    """
    edits = edits_of(row)["texts"]
    out: list[dict[str, Any]] = []
    for slot in slot_defaults(row):
        key = str(slot.get("key") or "")
        merged = dict(slot)
        if slot.get("role") == textslot.ROLE_AVATAR_NAME:
            group = key.removeprefix("avatar:")
            if avatar_names and group in avatar_names:
                merged["text"] = avatar_names[group]
            out.append(merged)
            continue
        override = edits.get(key)
        if isinstance(override, dict):
            for field in ("text", "font", "colour", "font_size"):
                value = override.get(field)
                if value not in (None, ""):
                    merged[field] = value
        out.append(merged)
    # Last, because the arrangement is about *where* these slots sit and it is the only step
    # that can change a rect without changing a character. Applied here rather than at each
    # render site so the panel, the still and the export cannot disagree (§19.38).
    from animatter.services import video_layout

    return video_layout.apply_to_slots(row, out)


def draw_slots(
    row: VideoTemplate,
    *,
    visible_avatars: set[str] | None = None,
    avatar_names: dict[str, str] | None = None,
) -> list[overlay_media.DrawSlot]:
    """The slots as the renderer wants them.

    ``visible_avatars`` is the set of avatar groups that will actually be drawn in the film. A
    name whose slot is not among them is skipped: an unbound slot disappears, and leaving its name
    behind puts 「彼方」 where an avatar used to be. ``None`` means "every slot is visible", which
    is the case when nothing is bound and the full poster is used.

    ``avatar_names`` replaces an avatar's name plate with its **speaker's** name (round 99).
    """
    out: list[overlay_media.DrawSlot] = []
    for slot in resolved_texts(row, avatar_names=avatar_names):
        if slot.get("role") == textslot.ROLE_AVATAR_NAME and visible_avatars is not None:
            group = str(slot.get("key") or "").removeprefix("avatar:")
            if group and group not in visible_avatars:
                continue
        # The designer's placeholder inside the box is not drawn: the footage belongs there.
        # It stays in the slot list (the UI says 「框内文字，暂不参与渲染」) but must never reach
        # the overlay, or the film prints 「本期嘉宾——」 over the guest's own picture.
        if slot.get("role") == textslot.ROLE_CONTENT:
            continue
        rect = slot.get("rect") or {}
        try:
            out.append(
                overlay_media.DrawSlot(
                    text=str(slot.get("text") or ""),
                    x=int(rect.get("x") or 0),
                    y=int(rect.get("y") or 0),
                    w=int(rect.get("w") or 0),
                    h=int(rect.get("h") or 0),
                    font_size=int(slot.get("font_size") or 0),
                    colour=str(slot.get("colour") or "#000000"),
                    font=str(slot.get("font") or fonts.TITLE_FONT),
                    # 期号 and 头像名 are one line by design; a 30 px name plate holding
                    # 「彼方（嘉宾）」 must grow right, not stack. Titles and notes are real
                    # multi-line blocks and keep wrapping inside their rect.
                    wrap=str(slot.get("role")) not in {"episode", "avatar_name"},
                )
            )
        except (TypeError, ValueError):
            # A slot whose geometry is unreadable is skipped rather than crashing a render:
            # one missing line of text is a visible, fixable defect, an exception is not.
            log.warning("template.slot_unreadable", key=slot.get("key"))
    return out


def update_text(
    session: Session,
    project_id: str,
    *,
    key: str,
    edit: TextEdit,
) -> VideoTemplate:
    """Store one text override (or clear it, with all-empty fields)."""
    row = template_service.require(session, project_id)
    defaults = {str(slot.get("key") or ""): slot for slot in slot_defaults(row)}
    if key not in defaults:
        raise AppError(
            "template_slot_unknown",
            f"模板里没有「{key}」这块文字",
            hint="可能是模板重新编译过，刷新页面再试。",
            status=404,
        )
    if defaults[key].get("role") == textslot.ROLE_AVATAR_NAME:
        # The plate says the bound speaker's name (round 99) — there is no field for it, so an
        # override would be a value nothing could show or clear. Refused loudly rather than
        # stored and ignored.
        raise AppError(
            "template_slot_derived",
            "头像名字来自绑定的说话人，不能直接改",
            hint="在头像那一行改绑定，或者先在字幕里把说话人改名。",
            status=409,
        )
    if edit.font is not None and edit.font.strip() and fonts.resolve(edit.font) is None:
        raise AppError(
            "font_missing",
            f"找不到字体「{edit.font}」",
            hint=f"可选：{'、'.join(fonts.families()[:8])}…",
            status=422,
        )

    data = edits_of(row)
    texts = dict(data["texts"])
    entry = dict(texts.get(key) or {}) if isinstance(texts.get(key), dict) else {}
    for field, value in (
        ("text", edit.text),
        ("font", edit.font),
        ("colour", edit.colour),
        ("font_size", edit.font_size),
    ):
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            entry.pop(field, None)
        else:
            entry[field] = value

    if entry:
        texts[key] = entry
    else:
        texts.pop(key, None)
    payload: dict[str, Any] = {"texts": texts, "photos": data["photos"]}
    if data.get("layout") is not None:
        payload["layout"] = data["layout"]
    row.edits_json = json.dumps(payload, ensure_ascii=False)
    session.flush()
    log.info("template.text_edited", template=row.id, key=key, fields=sorted(entry))
    return row


def _edit_dir(row: VideoTemplate) -> Path:
    """Where replacement avatar art goes: ``<build-dir>/edits``.

    Inside the compile output, not beside it. The asset route resolves file names by looking
    in exactly those two directories (``asset_path``), and putting the edits one level up made
    every replaced photo 404 while the JSON said it had worked — a green response and a broken
    image, which is the worst shape a bug can take here.

    Keyed by the PSD's hash for free, because that is what the build directory is named after:
    a *different* template gets a different directory and cannot inherit the old photos.
    """
    target = Path(row.background_path).parent / EDIT_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def _avatar_slot(row: VideoTemplate, name: str) -> dict[str, Any]:
    slots = _loads(row.slots_json)
    avatars = slots.get("avatars")
    items = [item for item in avatars if isinstance(item, dict)] if isinstance(avatars, list) else []
    for item in items:
        if item.get("name") == name:
            return item
    raise AppError(
        "template_slot_unknown",
        f"模板里没有头像位「{name}」",
        hint="刷新页面再试；模板可能刚被重新编译过。",
        status=404,
    )


def set_photo(
    session: Session,
    settings: Settings,
    project_id: str,
    *,
    slot: str,
    asset_id: str | None,
) -> VideoTemplate:
    """Replace (or restore) one avatar slot's photo.

    The compiled ``avatar<N>_frame.png`` / ``_mask.png`` pair is what makes this possible
    without the PSD: the replacement is cover-fitted into the mask's rect, clipped by the
    mask, and composited onto the frame. The result is written next to the build output and
    named per slot, so a second replacement overwrites the first instead of accumulating.

    ``asset_id=None`` restores the PSD's own photo by dropping the override — no file is
    written, because the compiled lit/dim PNGs are still exactly what the PSD had.
    """
    row = template_service.require(session, project_id)
    avatar = _avatar_slot(row, slot)
    data = edits_of(row)
    photos = dict(data["photos"])

    if asset_id is None:
        photos.pop(slot, None)
        payload: dict[str, Any] = {"texts": data["texts"], "photos": photos}
        if data.get("layout") is not None:
            payload["layout"] = data["layout"]
        row.edits_json = json.dumps(payload, ensure_ascii=False)
        session.flush()
        log.info("template.photo_cleared", template=row.id, slot=slot)
        return row

    asset = asset_service.require(session, asset_id)
    if asset.project_id != project_id:
        raise AppError("asset_not_found", "这个素材不属于当前项目", status=404)

    frame = Path(str(avatar.get("frame_path") or ""))
    mask = Path(str(avatar.get("mask_path") or ""))
    if not frame.is_file() or not mask.is_file():
        raise AppError(
            "template_photo_unsupported",
            "这个模板没有可替换头像所需的图层",
            hint="重新导入并编译一次模板；换图需要编译时切出的头像框和蒙版。",
            status=409,
        )

    mask_rect = avatar.get("mask_rect") or {}
    target = _edit_dir(row) / f"avatar-{_slug(slot)}-lit.png"
    dim_target = _edit_dir(row) / f"avatar-{_slug(slot)}-dim.png"
    overlay_media.fit_photo(
        source=Path(asset.path),
        frame=frame,
        mask=mask,
        mask_rect=(
            int(mask_rect.get("x") or 0),
            int(mask_rect.get("y") or 0),
            int(mask_rect.get("w") or 0),
            int(mask_rect.get("h") or 0),
        ),
        target=target,
    )
    overlay_media.dim(image_path=target, mask_path=mask, target=dim_target)

    photos[slot] = asset_id
    payload = {"texts": data["texts"], "photos": photos}
    if data.get("layout") is not None:
        payload["layout"] = data["layout"]
    row.edits_json = json.dumps(payload, ensure_ascii=False)
    session.flush()
    log.info("template.photo_set", template=row.id, slot=slot, asset=asset_id)
    return row


def _slug(name: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in name).strip("-")
    return cleaned[:40] or "slot"


def photo_paths(row: VideoTemplate, avatar: dict[str, Any]) -> tuple[Path, Path] | None:
    """The lit/dim PNGs to use for one slot, honouring a replacement when there is one.

    Returns ``None`` when nothing was replaced, so callers fall back to the compiled pair —
    the replacement is an *override*, and "no override" has to stay a cheap, exact path.
    """
    photos = edits_of(row)["photos"]
    slot = str(avatar.get("name") or "")
    if slot not in photos:
        return None
    lit = _edit_dir(row) / f"avatar-{_slug(slot)}-lit.png"
    dim = _edit_dir(row) / f"avatar-{_slug(slot)}-dim.png"
    if not lit.is_file() or not dim.is_file():
        log.warning("template.photo_missing", template=row.id, slot=slot)
        return None
    return lit, dim


def signature(row: VideoTemplate) -> str:
    """A short hash of the edits, for cache keys.

    Both the still preview and the export signature already carry a digest of what changes
    their pixels; the title is one of those things now. Leaving it out would reproduce the
    exact bug §19.11 trap 24 describes: the file is rewritten, the URL does not change, and
    the browser keeps showing the old frame.
    """
    digest = hashlib.sha256()
    # The drawing rules are an input to the pixels too. Without this, deploying a layout fix
    # leaves every still and every text PNG in place under an unchanged name — measured: a
    # wrap fix produced a byte-identical still because the digest only covered the data.
    digest.update(f"v{overlay_media.RENDER_VERSION}|".encode())
    data = edits_of(row)
    digest.update(json.dumps(data["texts"], sort_keys=True, ensure_ascii=False).encode())
    digest.update(b"|")
    digest.update(json.dumps(data["photos"], sort_keys=True, ensure_ascii=False).encode())
    digest.update(b"|")
    # The arrangement is drawn into the same PNG, so it belongs in the same key. A moved title
    # changes no character and no colour, which is exactly the case a signature made only of
    # those two things cannot see.
    from animatter.core import layout as layout_core

    digest.update(layout_core.parse(data.get("layout")).signature().encode())
    return digest.hexdigest()[:10]
