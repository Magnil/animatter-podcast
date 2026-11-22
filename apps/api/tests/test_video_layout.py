"""The layout overrides: what moves, what follows, and what must not change (§19.38).

The interesting property of this feature is not "a dragged box appears where you put it". It is
that an **untouched** project is pixel-identical after the feature ships, and that the four
places the layout becomes pixels (box, artwork, text slots, avatar frames) cannot disagree. So
the tests are grouped the same way:

* the arithmetic in ``core.layout`` — identity, clamping, the three derived placements;
* the service — what an empty layout means, what a recompile does to a stale key;
* HTTP — the endpoints the editor calls, including「恢复默认」.

The fixture is a template row with one avatar slot and three text slots (a title, an avatar name
plate, and a designer ``content`` slot that never renders and therefore cannot be dragged).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core import layout as layout_core
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.media import text_overlay
from animatter.models import VideoTemplate
from animatter.services import video_edits, video_layout, video_templates

CANVAS = (1920, 1080)
BOX = {"x": 240, "y": 209, "w": 1440, "h": 810}
PLATE_RECT = {"x": 238, "y": 206, "w": 1444, "h": 815}
STROKE_RECT = {"x": 231, "y": 198, "w": 1456, "h": 829}
TITLE_RECT = {"x": 1068, "y": 88, "w": 617, "h": 109}
NAME_RECT = {"x": 47, "y": 202, "w": 30, "h": 18}
AVATAR_RECT = {"x": 42, "y": 200, "w": 177, "h": 188}


@pytest.fixture()
def project_id(session: Session) -> str:
    version = seed_version(session, text="布局")
    session.commit()
    return version.project_id


def write_png(path: Path, size: tuple[int, int], colour=(20, 20, 20, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, colour).save(path)
    return path


def make_template(
    session: Session,
    project_id: str,
    build: Path,
    *,
    edits: dict[str, object] | None = None,
) -> VideoTemplate:
    """A compiled row with one avatar slot and three text slots."""
    build.mkdir(parents=True, exist_ok=True)
    write_png(build / "background.png", CANVAS)
    write_png(build / "background_open.png", CANVAS)
    write_png(build / "overlay.png", (1456, 829))
    write_png(build / "plate.png", (1444, 815), (244, 244, 244, 255))
    slots = {
        "avatars": [
            {
                "name": "头像1",
                "label_name": "主播1",
                "rect": dict(AVATAR_RECT),
                "photo": dict(AVATAR_RECT),
                "label": "主播1",
                "lit_path": str(build / "avatar0_lit.png"),
                "dim_path": str(build / "avatar0_dim.png"),
                "frame_path": str(build / "avatar0_frame.png"),
                "mask_path": str(build / "avatar0_mask.png"),
                "mask_rect": {"x": 17, "y": 30, "w": 201, "h": 202},
                "name_key": "avatar:头像1",
            }
        ],
        "texts": [
            {
                "key": "title",
                "role": "title",
                "label": "当期标题",
                "name": "标题层",
                "rect": dict(TITLE_RECT),
                "text": "标题",
                "font_size": 56,
                "colour": "#1947ba",
                "font": "Alimama ShuHeiTi",
            },
            {
                "key": "avatar:头像1",
                "role": "avatar_name",
                "label": "头像名",
                "name": "马小褂",
                "rect": dict(NAME_RECT),
                "text": "马小褂",
                "font_size": 16,
                "colour": "#1947ba",
                "font": "Alimama ShuHeiTi",
            },
            {
                "key": "note:嘉宾",
                "role": "content",
                "label": "框内文字",
                "name": "嘉宾",
                "rect": {"x": 928, "y": 392, "w": 243, "h": 36},
                "text": "嘉宾",
                "font_size": 28,
                "colour": "#1947ba",
                "font": "Alimama ShuHeiTi",
            },
        ],
        "notes": [],
        "art": {
            "plate": {"path": "plate.png", "rect": dict(PLATE_RECT)},
            "stroke": {"path": "overlay.png", "rect": dict(STROKE_RECT)},
        },
    }
    row = VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path=str(build / "source.psd"),
        source_sha256="0" * 64,
        canvas_w=CANVAS[0],
        canvas_h=CANVAS[1],
        background_path=str(build / "background.png"),
        overlay_path=str(build / "overlay.png"),
        box_rect=json.dumps(BOX),
        box_fit_default="contain",
        slots_json=json.dumps(slots, ensure_ascii=False),
        bindings_json="{}",
        edits_json=json.dumps(edits or {}, ensure_ascii=False),
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


# --- core arithmetic ----------------------------------------------------


def test_an_empty_layout_is_the_compiled_layout() -> None:
    empty = layout_core.Layout()
    assert empty.is_empty()
    # ``-`` and not ``""``: the empty signature goes into cache keys, and an empty string
    # inside ``"|".join`` would make two different keys collide.
    assert empty.signature() == "-"
    assert layout_core.effective_box(BOX, empty) == BOX


def test_the_same_payload_written_in_another_order_is_the_same_signature() -> None:
    first = layout_core.parse(
        {"texts": {"a": {"x": 1, "y": 2, "w": 3, "h": 4}, "b": {"x": 5, "y": 6, "w": 7, "h": 8}}}
    )
    second = layout_core.parse(
        {"texts": {"b": {"x": 5, "y": 6, "w": 7, "h": 8}, "a": {"x": 1, "y": 2, "w": 3, "h": 4}}}
    )
    assert first.signature() == second.signature()


def test_moving_the_box_scales_the_artwork_around_its_own_origin() -> None:
    half = layout_core.scale_art(
        PLATE_RECT, compiled_box=BOX, new_box={"x": 240, "y": 209, "w": 720, "h": 405}
    )
    assert half == {"x": 239, "y": 208, "w": 722, "h": 408}
    # Two pixels left of the box and three above it, at half scale: rounding, not truncation.
    assert half["x"] == 240 + round((PLATE_RECT["x"] - BOX["x"]) / 2)


def test_scaling_the_art_to_the_same_box_is_the_identity() -> None:
    assert (
        layout_core.scale_art(PLATE_RECT, compiled_box=BOX, new_box=BOX) == PLATE_RECT
    )
    assert layout_core.scale_art(STROKE_RECT, compiled_box=BOX, new_box=BOX) == STROKE_RECT


def test_a_name_plate_follows_its_avatar_and_nothing_else() -> None:
    moved = layout_core.Placement(x=AVATAR_RECT["x"] + 500, y=AVATAR_RECT["y"] + 40, w=177, h=188)
    shifted = layout_core.cascade_name_plate(
        NAME_RECT, compiled_avatar=AVATAR_RECT, moved=moved
    )
    assert shifted == {
        "x": NAME_RECT["x"] + 500,
        "y": NAME_RECT["y"] + 40,
        "w": NAME_RECT["w"],
        "h": NAME_RECT["h"],
    }
    untouched = layout_core.cascade_name_plate(
        NAME_RECT, compiled_avatar=AVATAR_RECT, moved=None
    )
    assert untouched == NAME_RECT


def test_the_type_follows_the_smaller_of_the_two_factors() -> None:
    """⑬: type may only grow as far as **both** dimensions allow.

    A box dragged 2× wider but left the same height would otherwise produce 112 px type inside a
    109 px-tall rect — the first line clipped and no error anywhere. So a one-axis drag does not
    scale at all, and a proportional corner drag scales exactly.
    """
    wider = layout_core.Placement(x=0, y=0, w=TITLE_RECT["w"] * 2, h=TITLE_RECT["h"])
    assert layout_core.scale_font(56, compiled=TITLE_RECT, moved=wider) == 56
    taller = layout_core.Placement(x=0, y=0, w=TITLE_RECT["w"], h=TITLE_RECT["h"] * 2)
    assert layout_core.scale_font(56, compiled=TITLE_RECT, moved=taller) == 56
    both = layout_core.Placement(x=0, y=0, w=TITLE_RECT["w"] * 2, h=TITLE_RECT["h"] * 2)
    assert layout_core.scale_font(56, compiled=TITLE_RECT, moved=both) == 112
    # Even, because the overlay's own fitting steps by 2 px: an odd request lands on the same
    # size as its even neighbour and the panel would show a number the picture does not have.
    half = layout_core.Placement(x=0, y=0, w=50, h=50)
    assert layout_core.scale_font(69, compiled={"w": 100, "h": 100}, moved=half) == 34


def test_a_rect_off_the_canvas_is_clamped_back_onto_it() -> None:
    clamped = layout_core.clamp_placement(
        layout_core.Placement(x=2500, y=-40, w=400, h=100),
        canvas_w=CANVAS[0],
        canvas_h=CANVAS[1],
        min_w=layout_core.MIN_TEXT_W,
        min_h=layout_core.MIN_TEXT_H,
    )
    assert (clamped.x, clamped.y) == (CANVAS[0] - 400, 0)
    # A box that is smaller than the minimum grows to it, and stays even for libx264.
    tiny = layout_core.clamp_placement(
        layout_core.Placement(x=0, y=0, w=3, h=3),
        canvas_w=CANVAS[0],
        canvas_h=CANVAS[1],
        min_w=layout_core.MIN_BOX_W,
        min_h=layout_core.MIN_BOX_H,
        even=True,
    )
    assert (tiny.w, tiny.h) == (layout_core.MIN_BOX_W, layout_core.MIN_BOX_H)
    assert tiny.w % 2 == 0 and tiny.h % 2 == 0


def test_malformed_json_degrades_to_the_compiled_layout() -> None:
    for raw in (None, "", "not json", "[]", 42, {"texts": "no"}, {"box": {"x": 1}}):
        parsed = layout_core.parse(raw)
        assert parsed.is_empty(), raw
    # A partially valid object keeps the parts that are rectangles.
    partial = layout_core.parse(
        {
            "box": {"x": 1, "y": 2, "w": 300, "h": 200},
            "texts": {"a": {"x": 1}, "b": {"x": 1, "y": 2, "w": 3, "h": 4}},
        }
    )
    assert partial.box is not None
    assert set(partial.texts) == {"b"}


def test_floats_and_numeric_strings_are_accepted() -> None:
    """JSON from a browser can hold ``100.4`` or ``"100"``; neither may fail an export."""
    parsed = layout_core.parse_placement({"x": 100.4, "y": "20", "w": 300, "h": "200"})
    assert parsed is not None
    assert (parsed.x, parsed.y, parsed.w, parsed.h) == (100, 20, 300, 200)


# --- service ------------------------------------------------------------


def test_a_fresh_template_has_no_overrides(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    assert video_layout.layout_of(row).is_empty()
    assert video_layout.box_of(row) == BOX
    plate, stroke = video_layout.art_of(row)
    assert plate is not None and stroke is not None
    assert plate.as_rect() == PLATE_RECT
    assert stroke.as_rect() == STROKE_RECT


def test_an_untouched_project_draws_exactly_what_the_compile_drew(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The invariant that makes this feature safe to ship: no override, no pixel change.

    ``apply_to_slots`` is called on the *render* path, so a bug that shifted a rect by one pixel
    would silently re-render every existing film.
    """
    row = make_template(session, project_id, tmp_path / "build")
    resolved = video_edits.resolved_texts(row)
    compiled = {item["key"]: item for item in video_edits.slot_defaults(row)}
    for slot in resolved:
        assert slot["rect"] == compiled[slot["key"]]["rect"]
        assert slot["font_size"] == compiled[slot["key"]]["font_size"]
    slots = video_layout.avatar_slots(row)
    assert [slot["rect"] for slot in slots] == [AVATAR_RECT]


def test_a_moved_text_slot_reaches_the_renderer(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session,
        project_id,
        texts={"title": {"x": 100, "y": 200, "w": 617, "h": 109}},
    )
    resolved = {slot["key"]: slot for slot in video_edits.resolved_texts(row)}
    assert resolved["title"]["rect"] == {"x": 100, "y": 200, "w": 617, "h": 109}
    # Same size, so the type does not move — a pure translation is not a resize.
    assert resolved["title"]["font_size"] == 56
    # Everything else stays where the compile put it.
    assert resolved["avatar:头像1"]["rect"] == NAME_RECT


def test_resizing_a_text_slot_scales_its_type(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session,
        project_id,
        texts={"title": {"x": 1068, "y": 88, "w": 1234, "h": 218}},
    )
    resolved = {slot["key"]: slot for slot in video_edits.resolved_texts(row)}
    # Both axes doubled, so the type doubles. Clamped to the canvas: 1068 + 1234 > 1920, so the
    # stored rect slid left and the *size* is what the factor reads.
    assert resolved["title"]["font_size"] == 112


def test_an_explicit_font_size_wins_over_the_scale(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The text panel's number box and the canvas editor share one stored value."""
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session,
        project_id,
        texts={"title": {"x": 1068, "y": 88, "w": 617, "h": 218, "font_size": 40}},
    )
    resolved = {slot["key"]: slot for slot in video_edits.resolved_texts(row)}
    assert resolved["title"]["font_size"] == 40


def test_the_name_plate_follows_a_moved_avatar(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session,
        project_id,
        avatars={"头像1": {"x": 542, "y": 240, "w": 177, "h": 188}},
    )
    slots = {slot["name"]: slot for slot in video_layout.avatar_slots(row)}
    assert slots["头像1"]["rect"] == {"x": 542, "y": 240, "w": 177, "h": 188}
    resolved = {slot["key"]: slot for slot in video_edits.resolved_texts(row)}
    # +500 right, +40 down, and the plate keeps its own 30×18.
    assert resolved["avatar:头像1"]["rect"] == {
        "x": NAME_RECT["x"] + 500,
        "y": NAME_RECT["y"] + 40,
        "w": NAME_RECT["w"],
        "h": NAME_RECT["h"],
    }


def test_a_content_slot_is_not_draggable(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The designer's placeholders never render, so offering a handle would be a lie."""
    row = make_template(session, project_id, tmp_path / "build")
    state = video_layout.editor_state(row)
    keys = {item["key"] for item in state["texts"]}
    assert "note:嘉宾" not in keys
    assert {"title", "avatar:头像1"} <= keys
    with pytest.raises(AppError) as caught:
        video_layout.save(
            session, project_id, texts={"note:嘉宾": {"x": 0, "y": 0, "w": 10, "h": 10}}
        )
    # ``_fixed``, not ``_unknown``: the slot exists and the user can see it in the panel, so
    # "模板里没有这块文字" would send them looking for a recompile that is not the problem.
    assert caught.value.code == "layout_slot_fixed"


def test_a_stale_key_from_another_template_stops_being_drawn(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """A recompile can legitimately remove a slot; the stored override must not resurrect it."""
    row = make_template(
        session,
        project_id,
        tmp_path / "build",
        edits={
            "texts": {},
            "photos": {},
            "layout": {"texts": {"note:gone": {"x": 1, "y": 2, "w": 30, "h": 40}}},
        },
    )
    assert video_layout.layout_of(row).texts == {}
    # …but it is still in the column, so switching to the PSD it belonged to brings it back.
    assert "note:gone" in video_edits.edits_of(row)["layout"]["texts"]


def test_moving_the_box_moves_the_plate_and_the_stroke(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session, project_id, box={"x": 240, "y": 209, "w": 1152, "h": 648}
    )
    plate, stroke = video_layout.art_of(row)
    assert plate is not None and stroke is not None
    # 0.8× around the box origin: the 2 px / 3 px offsets become 2 px / 2 px.
    assert plate.as_rect() == {"x": 238, "y": 207, "w": 1155, "h": 652}
    assert stroke.as_rect() == {"x": 233, "y": 200, "w": 1165, "h": 663}
    assert video_layout.box_of(row) == {"x": 240, "y": 209, "w": 1152, "h": 648}


def test_the_layout_is_folded_into_every_cache_key(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """Four digests read the arrangement; a missed one serves a stale frame forever."""
    row = make_template(session, project_id, tmp_path / "build")
    before = (
        video_edits.signature(row),
        video_layout.art_signature(row),
        video_layout.layout_of(row).signature(),
    )
    video_layout.save(
        session, project_id, texts={"title": {"x": 200, "y": 200, "w": 617, "h": 109}}
    )
    after = (
        video_edits.signature(row),
        video_layout.art_signature(row),
        video_layout.layout_of(row).signature(),
    )
    assert before[2] != after[2]
    assert before[0] != after[0], "the text PNG would keep the old arrangement"
    # The artwork did not move, so this one legitimately stays — it keys the bake, not the text.
    assert before[1] == after[1]


def test_art_signature_moves_with_the_box(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    before = video_layout.art_signature(row)
    video_layout.save(session, project_id, box={"x": 300, "y": 250, "w": 1200, "h": 700})
    assert video_layout.art_signature(row) != before


def test_a_text_edit_does_not_erase_the_arrangement(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """Three writers share one JSON column; each used to rebuild it from its own fields."""
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(
        session, project_id, texts={"title": {"x": 200, "y": 200, "w": 617, "h": 109}}
    )
    video_edits.update_text(session, project_id, key="title", edit=video_edits.TextEdit(text="新标题"))
    assert video_layout.layout_of(row).texts["title"].x == 200
    assert {slot["key"]: slot for slot in video_edits.resolved_texts(row)}["title"]["text"] == "新标题"


def test_a_layout_save_does_not_erase_a_text_edit(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_edits.update_text(session, project_id, key="title", edit=video_edits.TextEdit(text="新标题"))
    video_layout.save(
        session, project_id, texts={"title": {"x": 200, "y": 200, "w": 617, "h": 109}}
    )
    resolved = {slot["key"]: slot for slot in video_edits.resolved_texts(row)}
    assert resolved["title"]["text"] == "新标题"


def test_reset_forgets_every_override_and_nothing_else(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_edits.update_text(session, project_id, key="title", edit=video_edits.TextEdit(text="新标题"))
    video_layout.save(session, project_id, box={"x": 300, "y": 250, "w": 1200, "h": 700})
    video_layout.reset(session, project_id)
    assert video_layout.layout_of(row).is_empty()
    assert video_layout.box_of(row) == BOX
    # The title is an edit, not a layout — 「恢复默认布局」 must not rewrite the user's words.
    assert {slot["key"]: slot for slot in video_edits.resolved_texts(row)}["title"]["text"] == "新标题"


def test_replace_writes_the_whole_object(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(session, project_id, texts={"title": {"x": 100, "y": 100, "w": 617, "h": 109}})
    video_layout.save(session, project_id, texts={"title": TITLE_RECT}, replace=True)
    # A placement equal to the compiled one is **not stored**: an override means "this differs
    # from where the compile put it", so storing an identity would leave `is_empty()` false for
    # ever on a project nobody moved — and change all four cache keys for a save that changed no
    # pixel.
    assert video_layout.layout_of(row).is_empty()
    assert "layout" not in json.loads(row.edits_json)


def test_dragging_a_slot_back_to_where_it_was_forgets_the_override(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    moved = {"x": 100, "y": 100, "w": 617, "h": 109}
    video_layout.save(session, project_id, texts={"title": moved})
    assert not video_layout.layout_of(row).is_empty()
    video_layout.save(session, project_id, texts={"title": TITLE_RECT})
    assert video_layout.layout_of(row).is_empty()


def test_a_two_pixel_difference_still_counts_as_moved(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    # Rounding inside the editor can produce a rect a pixel or two off the compiled one. Two is a
    # real move: dropping it would make the stored layout disagree with the picture, and the drag
    # would look like it did nothing on reload.
    row = make_template(session, project_id, tmp_path / "build")
    nudged = {**TITLE_RECT, "x": TITLE_RECT["x"] + 2}
    video_layout.save(session, project_id, texts={"title": nudged})
    assert video_layout.layout_of(row).texts["title"].x == nudged["x"]


def test_a_template_that_is_not_ready_refuses_to_store_a_layout(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    row.status = "compiling"
    session.flush()
    with pytest.raises(AppError) as caught:
        video_layout.save(session, project_id, texts={"title": {"x": 1, "y": 2, "w": 300, "h": 40}})
    assert caught.value.code == "template_not_ready"


# --- HTTP ---------------------------------------------------------------


def test_the_editor_state_lists_every_draggable_slot(
    session: Session, project_id: str, tmp_path: Path, client: TestClient
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    response = client.get(f"/api/projects/{project_id}/video/layout")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["canvas"] == {"w": CANVAS[0], "h": CANVAS[1]}
    assert body["box"] == BOX and body["compiled_box"] == BOX
    assert body["plate"] == PLATE_RECT and body["stroke"] == STROKE_RECT
    assert body["is_default"] is True and body["changed"] == {}
    assert {item["key"] for item in body["texts"]} == {"title", "avatar:头像1"}
    assert [item["key"] for item in body["avatars"]] == ["头像1"]
    assert body["texts"][0]["font_size"] == 56


def test_put_moves_a_slot_and_get_reports_it(
    session: Session, project_id: str, tmp_path: Path, client: TestClient
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    response = client.put(
        f"/api/projects/{project_id}/video/layout",
        json={"texts": {"title": {"x": 120, "y": 140, "w": 617, "h": 109}}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_default"] is False
    moved = next(item for item in body["texts"] if item["key"] == "title")
    assert (moved["x"], moved["y"]) == (120, 140)
    assert body["box"] == BOX


def test_reset_endpoint_clears_everything(
    session: Session, project_id: str, tmp_path: Path, client: TestClient
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    client.put(
        f"/api/projects/{project_id}/video/layout",
        json={"box": {"x": 300, "y": 250, "w": 1200, "h": 700}},
    )
    response = client.post(f"/api/projects/{project_id}/video/layout/reset")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_default"] is True
    assert body["box"] == BOX
    assert body["plate"] == PLATE_RECT


def test_put_rejects_an_unknown_slot_with_a_readable_reason(
    session: Session, project_id: str, tmp_path: Path, client: TestClient
) -> None:
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    response = client.put(
        f"/api/projects/{project_id}/video/layout",
        json={"texts": {"title-nope": {"x": 0, "y": 0, "w": 300, "h": 40}}},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "layout_slot_unknown"


def test_put_clamps_instead_of_refusing(
    session: Session, project_id: str, tmp_path: Path, client: TestClient
) -> None:
    """The drag already happened on the user's screen; refusing it makes the two disagree."""
    make_template(session, project_id, tmp_path / "build")
    session.commit()
    response = client.put(
        f"/api/projects/{project_id}/video/layout",
        json={"texts": {"title": {"x": 5000, "y": -100, "w": 617, "h": 109}}},
    )
    assert response.status_code == 200, response.text
    moved = next(item for item in response.json()["texts"] if item["key"] == "title")
    assert moved["x"] == CANVAS[0] - 617
    assert moved["y"] == 0


def test_the_layout_endpoint_needs_a_template(client: TestClient, session: Session) -> None:
    version = seed_version(session, text="没有模板")
    session.commit()
    response = client.get(f"/api/projects/{version.project_id}/video/layout")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "video_template_missing"


def test_the_text_png_name_changes_when_a_slot_moves(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """The still and the panel read a cached PNG **by name**; a moved rect must rename it."""
    row = make_template(session, project_id, tmp_path / "build")
    # Only the signature is under test here — drawing needs the real fonts, and a missing one
    # would fail for a reason that has nothing to do with the layout.
    before = text_overlay.signature(video_edits.draw_slots(row))
    video_layout.save(
        session, project_id, texts={"title": {"x": 200, "y": 200, "w": 617, "h": 109}}
    )
    after = text_overlay.signature(video_edits.draw_slots(row))
    assert before != after


def test_editor_state_is_one_payload_for_the_whole_screen(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(session, project_id, avatars={"头像1": {"x": 542, "y": 240, "w": 177, "h": 188}})
    state = video_layout.editor_state(row)
    assert state["is_default"] is False
    assert state["changed"]["avatars"]["头像1"]["x"] == 542
    avatar = state["avatars"][0]
    assert (avatar["x"], avatar["y"]) == (542, 240)
    assert avatar["label"] == "主播1"
    # The name plate moved with its frame, and the editor sees the same number the render does.
    plate = next(item for item in state["texts"] if item["key"] == "avatar:头像1")
    assert plate["x"] == NAME_RECT["x"] + 500


def test_the_editor_carries_the_poster_and_the_artwork_urls(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """Decision ⑱: the canvas shows the film's own background, not a grey rectangle.

    Three URLs and not one composited image: the artwork has to end up *under* the handles, and
    a second bake of the same poster for the editor would be a fifth thing that can drift from
    what the export draws.
    """
    row = make_template(session, project_id, tmp_path / "build")
    state = video_layout.editor_state(row)
    assert state["background_url"].endswith("/video/template/asset/background.png")
    assert state["plate_url"].endswith("/video/template/asset/plate.png")
    assert state["stroke_url"].endswith("/video/template/asset/overlay.png")
    assert state["avatars"][0]["lit_url"].endswith("/video/template/asset/avatar0_lit.png")


def test_a_template_that_is_not_ready_serves_no_images(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    """``asset_url`` returns ``""`` for a row that is not ready; the editor must cope."""
    row = make_template(session, project_id, tmp_path / "build")
    row.status = "compiling"
    session.flush()
    state = video_layout.editor_state(row)
    assert state["background_url"] == ""
    assert state["plate_url"] == ""


def test_the_editor_lists_the_compiled_geometry_alongside(
    session: Session, project_id: str, tmp_path: Path
) -> None:
    row = make_template(session, project_id, tmp_path / "build")
    video_layout.save(session, project_id, box={"x": 240, "y": 209, "w": 1152, "h": 648})
    state = video_layout.editor_state(row)
    assert state["box"] != state["compiled_box"]
    assert state["compiled_box"] == BOX
    # So that a single slot can be restored without a round trip.
    assert video_templates.slot_list(row)[0]["rect"] == AVATAR_RECT
