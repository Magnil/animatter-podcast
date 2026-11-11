"""The avatar name plate is the **bound speaker's** name (round 99).

Round 98 made the plate an editable text field of its own, which meant 「主播1」 existed twice in
the panel (a name input and a binding row) and the poster could print 「彼方」 over 呈AA's face.
The field is gone: the plate is derived from the binding, so the two facts cannot disagree.

What is pinned here:

* a bound slot's plate is the speaker's name, not the PSD's placeholder;
* an unbound slot keeps the placeholder — it is only drawn when *nothing* is bound;
* renaming the speaker renames the plate, because there is no second copy to update;
* a stored ``avatar:`` text override is **ignored** and cannot be written — a value nothing can
  show or clear is worse than no value.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session
from tests.test_video_avatars import SLOTS, with_speakers

from animatter.errors import AppError
from animatter.models import VideoTemplate
from animatter.services import video_avatars, video_edits


def _row(session: Session, version: object, *, bindings: dict[str, int] | None = None) -> VideoTemplate:
    row = VideoTemplate(
        id="vtpl_names",
        project_id=version.project_id,  # type: ignore[attr-defined]
        source_path="C:/tmp/source.psd",
        source_sha256="0" * 64,
        canvas_w=1920,
        canvas_h=1080,
        background_path="C:/tmp/build/background.png",
        overlay_path="",
        box_rect=json.dumps({"x": 240, "y": 209, "w": 1440, "h": 810}),
        box_fit_default="blur_fill",
        slots_json=json.dumps(
            {
                "avatars": SLOTS["avatars"],
                "texts": [
                    {
                        "key": f"avatar:{slot['name']}",
                        "role": "avatar_name",
                        "label": slot["name"],
                        "rect": {"x": 10, "y": 10, "w": 60, "h": 20},
                        "text": slot["label"],
                        "font_size": 16,
                        "colour": "#f9485c",
                        "font": "Alimama ShuHeiTi",
                        "name": slot["label"],
                    }
                    for slot in SLOTS["avatars"]
                ],
            },
            ensure_ascii=False,
        ),
        edits_json="{}",
        bindings_json=json.dumps(bindings or {}, ensure_ascii=False),
        status="ready",
        error="",
        created_at="2026-11-12T00:00:00Z",
    )
    session.add(row)
    session.flush()
    return row


def test_the_plate_is_the_bound_speakers_name(session: Session) -> None:
    """头像1's PSD default is 「彼方」; bound to 马小褂 it must read 马小褂."""
    version = with_speakers(session, turns=[(1, 0, 1000)])
    row = _row(session, version, bindings={"头像1": 1})

    plates = video_avatars.name_plates(session, row)
    assert plates == {"头像1": "马小褂"}
    texts = {
        str(slot["key"]): str(slot["text"])
        for slot in video_edits.resolved_texts(row, avatar_names=plates)
    }
    assert texts["avatar:头像1"] == "马小褂"
    # The unbound slot is not touched: it keeps the designer's placeholder.
    assert texts["avatar:头像2"] == "马小褂"


def test_an_unbound_slot_keeps_the_psd_placeholder(session: Session) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = _row(session, version)

    assert video_avatars.name_plates(session, row) == {}
    texts = [str(slot["text"]) for slot in video_edits.resolved_texts(row)]
    assert texts == ["彼方", "马小褂"]


def test_renaming_the_speaker_renames_the_plate(session: Session) -> None:
    """The point of deriving it: one edit, one place, no second copy to go stale."""
    from animatter.services import speakers as speaker_service

    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = _row(session, version, bindings={"头像2": 0})
    assert video_avatars.name_plates(session, row) == {"头像2": "彼方"}

    speaker_service.rename_speaker(session, version.project_id, 0, "小榕")
    assert video_avatars.name_plates(session, row) == {"头像2": "小榕"}


def test_a_stored_plate_override_is_ignored_not_merely_outranked(session: Session) -> None:
    """Round 98 wrote these; they must stop changing the picture.

    Ignoring rather than outranking matters for the **unbound** case: a text override that laid
    dormant while the slot was bound would pop back the moment it was unbound, and the user
    would have no field to clear it with.
    """
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = _row(session, version)
    row.edits_json = json.dumps(
        {"texts": {"avatar:头像1": {"text": "旧名字", "font_size": 40}}, "photos": {}},
        ensure_ascii=False,
    )
    session.flush()

    texts = [str(slot["text"]) for slot in video_edits.resolved_texts(row)]
    assert "旧名字" not in texts
    # The size override goes with it: the row would otherwise change shape for a text nobody
    # can see, which is the same ghost in a different column.
    assert [slot["font_size"] for slot in video_edits.resolved_texts(row)] == [16, 16]


def test_writing_a_plate_override_is_refused(session: Session) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = _row(session, version)

    with pytest.raises(AppError) as caught:
        video_edits.update_text(
            session, row.project_id, key="avatar:头像1", edit=video_edits.TextEdit(text="x")
        )
    assert caught.value.code == "template_slot_derived"
    assert caught.value.status == 409
