"""Export identity and still-preview identity (P1/P3/P4, §19.5).

Two hashes decide whether the user sees a *new* film / a *new* frame:

* ``_video_signature`` — the export's identity. It exists so an identical film can be
  reused and a changed one cannot. Anything that changes pixels must be in it, or the
  cache silently hands back the previous encode.
* ``_still_digest`` — the still's file name. It must change exactly when the picture would,
  because the browser only re-fetches a URL it has not seen.

Both were wrong in the same direction: they omitted something the renderer reads.
``bindings_json`` (which speaker each avatar shows) was missing from the export hash while
nothing reused exports — harmless then, a wrong film the moment reuse lands. The still had
*no* digest at all: ``still-{at_ms}.png``, so tuning 字幕字号 rewrote the bytes under an
unchanged URL and the browser kept showing the old frame (measured: 1 request after two
bakes).
"""

from __future__ import annotations

import json
from pathlib import Path

from animatter.core.edl import Deletion, Edl, Keep
from animatter.core.video_avatar import Interval
from animatter.ids import new_id, utcnow_iso
from animatter.media import box_track
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.models import VideoItem, VideoTemplate
from animatter.services import video_box
from animatter.services import video_render as render

#: No DB anywhere in this module: both hashes are functions of plain dataclasses, and the
#: point of the tests is to pin *what is in them*, not to exercise the queries that build
#: them. A `project_id` string is all either one needs.
PROJECT = "prj_signature"


def edl_without_deletions(total_ms: int) -> Edl:
    return Edl(
        original_duration_ms=total_ms,
        keeps=(Keep(orig_start=0, orig_end=total_ms, new_start=0),),
        deletions=(Deletion(start_ms=0, end_ms=0, source="test"),),
    )


def make_template(project_id: str, *, bindings: str = "{}") -> VideoTemplate:
    return VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path="",
        source_sha256="a" * 64,
        canvas_w=1920,
        canvas_h=1080,
        background_path="",
        overlay_path="",
        box_rect=json.dumps({"x": 240, "y": 209, "w": 1440, "h": 810}),
        box_fit_default="blur_fill",
        slots_json="{}",
        bindings_json=bindings,
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )


def make_item(project_id: str, *, start_ms: int = 0, end_ms: int = 10_000) -> VideoItem:
    return VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind="media",
        start_ms=start_ms,
        end_ms=end_ms,
        z=0,
        fit="blur_fill",
        motion="none",
        asset_id="vasset_x",
        deck_id=None,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )


def prepared(
    project_id: str,
    *,
    bindings: str = "{}",
    items: list[VideoItem] | None = None,
    avatars: list[render.AvatarTrack] | None = None,
) -> render.PreparedVideo:
    total = 10_000
    return render.PreparedVideo(
        template=make_template(project_id, bindings=bindings),
        box_rect={"x": 240, "y": 209, "w": 1440, "h": 810},
        items=items if items is not None else [make_item(project_id)],
        edl=edl_without_deletions(total),
        duration_ms=total,
        avatars=avatars or [],
    )


def track(name: str) -> render.AvatarTrack:
    return render.AvatarTrack(
        name=name,
        x=42,
        y=200,
        width=177,
        height=188,
        lit_path=Path("lit.png"),
        dim_path=Path("dim.png"),
        lit=[],
    )


# --- export identity ----------------------------------------------------


def test_the_signature_is_stable_for_the_same_inputs() -> None:
    options = render.ExportOptions()
    assert render._video_signature(prepared(PROJECT), options) == render._video_signature(
        prepared(PROJECT), options
    )


def test_rebinding_an_avatar_changes_the_signature() -> None:
    """Which speaker a slot shows decides whether it ever goes dim — a pixel change.

    It was missing while every export queued a fresh encode, so nothing broke; it would
    break the first time an export got reused, and the symptom would be "I changed the
    binding and the film did not change", with no error anywhere.
    """
    options = render.ExportOptions()
    before = render._video_signature(prepared(PROJECT, bindings="{}"), options)
    after = render._video_signature(
        prepared(PROJECT, bindings='{"头像1": 0}'), options
    )
    assert before != after


def test_the_avatar_slots_in_play_change_the_signature() -> None:
    options = render.ExportOptions()
    before = render._video_signature(prepared(PROJECT), options)
    after = render._video_signature(prepared(PROJECT, avatars=[track("头像1")]), options)
    assert before != after


def test_moving_a_block_changes_the_signature() -> None:
    options = render.ExportOptions()
    before = render._video_signature(prepared(PROJECT), options)
    moved = prepared(PROJECT, items=[make_item(PROJECT, start_ms=2_000, end_ms=12_000)])
    assert render._video_signature(moved, options) != before


def test_the_subtitle_size_changes_the_signature() -> None:
    plain = render._video_signature(prepared(PROJECT), render.ExportOptions())
    big = render._video_signature(
        prepared(PROJECT),
        render.ExportOptions(subtitle_style=SubtitleStyle(font_size=88)),
    )
    assert plain != big


# --- still identity -----------------------------------------------------


def plan_of(items: list[VideoItem]) -> video_box.BoxPlan:
    """A plan built by hand, not by ``video_box.plan``.

    ``plan`` resolves assets through a session, and this module deliberately has no database:
    both hashes are functions of plain dataclasses, and what the tests pin is *what is in
    them*, not the queries that build them.
    """
    segments = [
        box_track.BoxSegment(
            input_index=index,
            duration_ms=item.end_ms - item.start_ms,
            kind=box_track.KIND_IMAGE,
            fit=item.fit,
        )
        for index, item in enumerate(items)
    ]
    return video_box.BoxPlan(
        segments=segments,
        sources=[Path(f"asset-{index}.png") for index in range(len(items))],
        width=1440,
        height=810,
        box_x=240,
        box_y=209,
        orphaned=0,
        empty=not segments,
    )


def digest(
    *,
    style: SubtitleStyle | None = None,
    at_ms: int = 3_000,
    items: list[VideoItem] | None = None,
    avatars: list[render.AvatarTrack] | None = None,
    bindings: str = "{}",
    burn: bool = True,
) -> str:
    template = make_template(PROJECT, bindings=bindings)
    built = items if items is not None else [make_item(PROJECT)]
    return render._still_digest(
        style or SubtitleStyle(),
        at_ms,
        plan_of(built),
        avatars or [],
        template,
        burn,
    )


def test_the_still_digest_is_stable_for_the_same_frame() -> None:
    assert digest() == digest()


def test_the_subtitle_size_changes_the_still_digest() -> None:
    """The bug: an unchanged URL meant the browser never re-fetched the new bytes."""
    assert digest() != digest(style=SubtitleStyle(font_size=88))


def test_the_bottom_margin_changes_the_still_digest() -> None:
    assert digest() != digest(style=SubtitleStyle(margin_v=140))


def test_the_instant_changes_the_still_digest() -> None:
    assert digest(at_ms=3_000) != digest(at_ms=4_000)


def test_burning_subtitles_or_not_changes_the_still_digest() -> None:
    assert digest() != digest(burn=False)


def test_a_block_edge_moves_the_still_digest_at_that_edge() -> None:
    """A frame inside the block must change when the block's content duration changes.

    The digest reads the *covering segment*, not the whole timeline, which is what keeps an
    unrelated edit from invalidating every still.
    """
    before = digest(at_ms=3_000)
    after = digest(at_ms=3_000, items=[make_item(PROJECT, start_ms=0, end_ms=6_000)])
    assert before != after


def test_an_unrelated_block_does_not_change_a_still_outside_it() -> None:
    """Otherwise every edit would re-render every still and the cache would be pointless."""
    only_first = [make_item(PROJECT, start_ms=0, end_ms=4_000)]
    with_second = [
        make_item(PROJECT, start_ms=0, end_ms=4_000),
        make_item(PROJECT, start_ms=8_000, end_ms=10_000),
    ]
    assert digest(at_ms=2_000, items=only_first) == digest(
        at_ms=2_000, items=with_second
    )


def test_the_bindings_change_the_still_digest() -> None:
    assert digest() != digest(bindings='{"头像1": 0}')


def test_the_box_source_file_name_changes_the_still_digest() -> None:
    """A slide's PNG is named after its size *and its typefaces* (§19.16).

    So "the deck's font changed" reaches the still only through the file name — the segment's
    kind, duration and crop are identical. Without this, changing a deck's font left the still
    byte-identical under an unchanged URL and the browser kept showing the old face: the same
    trap 24 that the subtitle style is already in the digest for.
    """
    template = make_template(PROJECT)
    segments = [
        box_track.BoxSegment(
            input_index=0,
            duration_ms=10_000,
            kind=box_track.KIND_IMAGE,
            fit="contain",
        )
    ]

    def digest_for(name: str) -> str:
        return render._still_digest(
            SubtitleStyle(),
            3_000,
            video_box.BoxPlan(
                segments=segments,
                sources=[Path(name)],
                width=1440,
                height=810,
                box_x=240,
                box_y=209,
                orphaned=0,
                empty=False,
            ),
            [],
            template,
            True,
        )

    assert digest_for("sld_a-1440x810-microsoft-yahei+microsoft-yahei.png") != digest_for(
        "sld_a-1440x810-simhei+microsoft-yahei.png"
    )
    # The directory is not part of the pixels: a per-run temp path must not move the digest.
    assert (
        digest_for("sld_a-1440x810-simhei.png")
        == render._still_digest(
            SubtitleStyle(),
            3_000,
            video_box.BoxPlan(
                segments=segments,
                sources=[Path("/elsewhere/sld_a-1440x810-simhei.png")],
                width=1440,
                height=810,
                box_x=240,
                box_y=209,
                orphaned=0,
                empty=False,
            ),
            [],
            template,
            True,
        )
    )


def test_the_avatar_state_at_that_instant_changes_the_still_digest() -> None:
    lit_now = render.AvatarTrack(
        name="头像1",
        x=42,
        y=200,
        width=177,
        height=188,
        lit_path=Path("lit.png"),
        dim_path=Path("dim.png"),
        lit=[Interval(start_ms=0, end_ms=5_000)],
    )
    dim_now = render.AvatarTrack(
        name="头像1",
        x=42,
        y=200,
        width=177,
        height=188,
        lit_path=Path("lit.png"),
        dim_path=Path("dim.png"),
        lit=[Interval(start_ms=0, end_ms=1_000)],
    )
    assert digest(at_ms=3_000, avatars=[lit_now]) != digest(
        at_ms=3_000, avatars=[dim_now]
    )
