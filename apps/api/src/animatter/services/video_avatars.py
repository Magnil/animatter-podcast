"""Avatar slot → speaker bindings, plus the lit/dim timeline they imply (P3, §19.5).

The binding lives in its **own column**, not inside ``slots_json``. ``slots_json`` is
rewritten from the PSD on every compile, so a binding stored there would silently vanish
the first time someone re-imports the same template — and the symptom would be "the avatars
stopped reacting" three sessions later, with nothing in the UI to explain it.

Two decisions worth stating:

* **A slot with no binding stays dim.** Not lit, not hidden: the PSD's own photo is what the
  compiled background shows, and that is a plausible panel. Lighting every unbound slot
  would make all four panel members look like they are talking at once, which is worse than
  none of them reacting.
* **A speaker may own more than one slot, and a slot may be bound to a speaker who never
  speaks.** Both are normal (`4 slots / 2 speakers` on the sample template), so the renderer
  builds no track for a slot that never lights up — a constant still is enough.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.core.edl import Edl
from animatter.core.video_avatar import (
    Interval,
    any_lit,
    merge,
)
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Sentence, Speaker, Token, TranscriptVersion, VideoTemplate
from animatter.services import video_templates as template_service

log = get_logger(__name__)


@dataclass(frozen=True)
class SlotBinding:
    """One avatar slot and the speaker it shows, if any.

    ``width``/``height`` are the slot's **placement** size, not the PNG's: the compiled
    lit/dim PNGs are the group's own size (236×249 on the sample) and get scaled to the
    slot rect (177×188), which is exactly what the compile step did when it flattened them
    into ``background.png``. Recomposing bare + all four lit PNGs at these rects matches
    ``background.png`` in **0** pixels, so the scaling cannot drift between the two bases.
    """

    name: str
    label: str
    x: int
    y: int
    width: int
    height: int
    lit_path: Path
    dim_path: Path
    speaker_id: int | None


def bindings_of(row: VideoTemplate) -> dict[str, int]:
    """Read the stored bindings, tolerating anything malformed.

    A hand-edited or half-written JSON blob must not break an export: an unreadable binding
    degrades to "nothing is bound", which renders the film with static avatars instead of
    failing after minutes of encoding.
    """
    try:
        parsed = json.loads(row.bindings_json or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for name, value in parsed.items():
        if not isinstance(name, str):
            continue
        try:
            local_id = int(value)
        except (TypeError, ValueError):
            continue
        if local_id >= 0:
            out[name] = local_id
    return out


def set_binding(
    session: Session, project_id: str, *, slot: str, speaker_id: int | None
) -> VideoTemplate:
    """Bind (or unbind, with ``None``) one avatar slot to a speaker.

    Validates the speaker against the project rather than trusting the id: a stale id from
    another project would light the wrong slot and there would be no way to see why.
    """
    row = template_service.require(session, project_id)
    slots = _slots_of(row)
    known = {slot_data["name"] for slot_data in slots}
    if slot not in known:
        raise AppError(
            "avatar_slot_unknown",
            f"这个模板里没有头像位「{slot}」",
            hint=f"可选：{'、'.join(sorted(known)) or '（无）'}。",
            status=404,
        )

    if speaker_id is not None:
        exists = session.scalars(
            select(Speaker).where(
                Speaker.project_id == project_id, Speaker.local_id == speaker_id
            )
        ).first()
        if exists is None:
            raise AppError(
                "speaker_not_found",
                f"项目里没有编号 {speaker_id} 的说话人",
                hint="先在字幕列表里给句子标上说话人。",
                status=404,
            )

    current = bindings_of(row)
    if speaker_id is None:
        current.pop(slot, None)
    else:
        current[slot] = speaker_id
    row.bindings_json = json.dumps(current, ensure_ascii=False)
    session.flush()
    log.info("avatar.bound", project=project_id, slot=slot, speaker=speaker_id)
    return row


def name_plates(session: Session, row: VideoTemplate) -> dict[str, str]:
    """What each **bound** slot's name plate should say: its speaker's name (round 99).

    The plate used to be an editable text field of its own, which is why the panel showed
    「主播1」 twice and why the poster could print 「彼方」 over 呈AA's face. Deriving it from the
    binding makes the two facts one: the plate names whoever is actually in the frame, and
    renaming a speaker renames the plate.

    Unbound slots are **absent** rather than empty: the caller keeps the PSD's placeholder,
    which is also what the full poster (nothing bound) is supposed to show. A binding whose
    speaker row has been deleted is absent for the same reason — printing the local id
    「说话人3」 would be worse than leaving the designer's name.
    """
    bindings = bindings_of(row)
    if not bindings:
        return {}
    names = {
        speaker.local_id: speaker.name
        for speaker in session.scalars(
            select(Speaker).where(Speaker.project_id == row.project_id)
        ).all()
    }
    out: dict[str, str] = {}
    for slot, local_id in bindings.items():
        name = str(names.get(local_id) or "").strip()
        if name:
            out[slot] = name
    return out


def _slots_of(row: VideoTemplate) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(row.slots_json or "{}")
    except ValueError:
        return []
    avatars = parsed.get("avatars") if isinstance(parsed, dict) else None
    return [item for item in avatars or [] if isinstance(item, dict) and "name" in item]


def bound_slots(session: Session, row: VideoTemplate) -> list[SlotBinding]:
    """Every avatar slot with its geometry and its speaker, bound or not.

    ``lit_path``/``dim_path`` are the **effective** art: the compiled PNGs, unless the user
    replaced this slot's photo, in which case the replacement pair. Deciding it here rather
    than at each render site is what keeps the preview, the avatar track and the export from
    disagreeing — the same reason ``_base_background`` exists.
    """
    from animatter.services import video_edits

    bindings = bindings_of(row)
    out: list[SlotBinding] = []
    for slot in _slots_of(row):
        name = str(slot.get("name") or "")
        rect = slot.get("rect") or {}
        lit = str(slot.get("lit_path") or "")
        dim = str(slot.get("dim_path") or "")
        if not name or not lit or not dim:
            continue
        replaced = video_edits.photo_paths(row, slot)
        if replaced is not None:
            lit, dim = str(replaced[0]), str(replaced[1])
        out.append(
            SlotBinding(
                name=name,
                label=str(slot.get("label") or ""),
                x=int(rect.get("x") or 0),
                y=int(rect.get("y") or 0),
                width=int(rect.get("w") or 0),
                height=int(rect.get("h") or 0),
                lit_path=Path(lit),
                dim_path=Path(dim),
                speaker_id=bindings.get(name),
            )
        )
    return out


def speaking_intervals(
    session: Session,
    version: TranscriptVersion,
    edl: Edl,
    speaker_id: int,
) -> list[Interval]:
    """When ``speaker_id`` talks, **in output time**.

    Mapped through the EDL rather than read raw: the film is shorter than the transcript
    wherever audio was cut, and an avatar that lights up on deleted audio would be lit while
    nobody is speaking. Deleted instants map to ``None`` and contribute nothing, which is
    exactly right — the avatar stays dim there.
    """
    rows = session.scalars(
        select(Sentence)
        .where(Sentence.version_id == version.id, Sentence.speaker_id == speaker_id)
        .order_by(Sentence.ord)
    ).all()
    if not rows:
        return []

    tokens = {
        token.idx: token
        for token in session.scalars(
            select(Token).where(Token.version_id == version.id)
        ).all()
    }
    time_map = edl.time_map() if edl.removed_ms else None

    intervals: list[Interval] = []
    for sentence in rows:
        # ``token_end`` is exclusive, so the last token is at ``token_end - 1``. Using
        # ``token_end`` directly takes the *next* sentence's first token and makes every
        # avatar run two sentences long — the kind of error that looks like lag.
        first = tokens.get(sentence.token_start)
        last = tokens.get(sentence.token_end - 1)
        if first is None or last is None:
            continue
        start_ms, end_ms = first.start_ms, last.end_ms
        if end_ms <= start_ms:
            continue
        if time_map is None:
            intervals.append(Interval(start_ms, end_ms))
            continue
        mapped = time_map.map_range(start_ms, end_ms)
        if mapped is not None and mapped[1] > mapped[0]:
            intervals.append(Interval(mapped[0], mapped[1]))
    return merge(intervals, total_ms=edl.output_duration_ms)


def resolve(
    session: Session,
    row: VideoTemplate,
    version: TranscriptVersion,
    edl: Edl,
) -> dict[str, list[Interval]]:
    """Lit intervals per **bound** slot, keyed by slot name.

    Unbound slots are absent from the result, which is what tells the renderer to leave the
    compiled background (with its static photos) alone for them.
    """
    out: dict[str, list[Interval]] = {}
    for slot in bound_slots(session, row):
        if slot.speaker_id is None:
            continue
        pieces = speaking_intervals(session, version, edl, slot.speaker_id)
        if any_lit(pieces, total_ms=edl.output_duration_ms):
            out[slot.name] = pieces
    return out


def describe(
    session: Session,
    row: VideoTemplate,
    version: TranscriptVersion,
    edl: Edl,
) -> list[dict[str, object]]:
    """What the confirm screen needs: each slot, its speaker, and how much it talks."""
    speakers = {
        item.local_id: item.name
        for item in session.scalars(
            select(Speaker).where(Speaker.project_id == row.project_id)
        ).all()
    }
    out: list[dict[str, object]] = []
    for slot in bound_slots(session, row):
        intervals = (
            speaking_intervals(session, version, edl, slot.speaker_id)
            if slot.speaker_id is not None
            else []
        )
        lit = sum(item.duration_ms for item in intervals)
        out.append(
            {
                "name": slot.name,
                "label": slot.label,
                "x": slot.x,
                "y": slot.y,
                "lit_url": _asset_url(row.project_id, slot.lit_path),
                "dim_url": _asset_url(row.project_id, slot.dim_path),
                "speaker_id": slot.speaker_id,
                # ``or -1`` would be wrong: speaker 0 is the first speaker, and
                # ``0 or -1`` is ``-1``. That would leave the host's name blank on every
                # project, which reads as "the binding did not take".
                "speaker_name": speakers.get(slot.speaker_id, "")
                if slot.speaker_id is not None
                else "",
                "turns": len(intervals),
                "lit_ms": lit,
                "share": round(lit / max(1, edl.output_duration_ms), 4),
            }
        )
    return out


def _asset_url(project_id: str, path: Path) -> str:
    return template_service.asset_url(project_id, str(path), "ready")
