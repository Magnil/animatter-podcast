"""Block → chain planning: where "video follows audio" is decided (§19.3, §19.5).

The mapping is the crux, so the tests aim at the three cases that break it:

1. **A deletion inside a block.** The block's audio is shorter, so its content plays for
   less; the block must not move.
2. **A block whose audio was deleted in full.** ``map_range`` returns ``None``; it must be
   counted as orphaned and contribute nothing — not crash, and not silently become a gap
   that looks like a bug.
3. **A gap.** An overlay input must span the whole film, so gaps become plate excerpts and
   the chain's total must equal the output length *exactly*. A chain one frame short ends
   with a frozen frame; a chain one frame long pushes everything after it.

Everything here runs without ffmpeg: the plan is arithmetic, and the encoder only ever sees
its result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.edl import Deletion, Edl, Keep
from animatter.ids import new_id, utcnow_iso
from animatter.media.box_track import (
    KIND_IMAGE,
    KIND_PLATE,
    KIND_TRANSPARENT,
    KIND_VIDEO,
    build_canvas_chain,
)
from animatter.models import VideoAsset, VideoItem
from animatter.services import video_box

BOX = {"x": 240, "y": 209, "w": 1440, "h": 810}


def edl_of(*keeps: tuple[int, int], original: int) -> Edl:
    """An EDL from explicit keeps, so the tests state the cut they mean."""
    built: list[Keep] = []
    cursor = 0
    for start, end in keeps:
        built.append(Keep(orig_start=start, orig_end=end, new_start=cursor))
        cursor += end - start
    return Edl(
        original_duration_ms=original,
        keeps=tuple(built),
        deletions=(Deletion(start_ms=0, end_ms=0, source="test"),),
    )


def make_asset(
    session: Session,
    project_id: str,
    tmp_path: Path,
    *,
    kind: str = KIND_IMAGE,
    duration_ms: int = 0,
    has_audio: bool = False,
) -> VideoAsset:
    path = tmp_path / f"{new_id('f')}.png"
    path.write_bytes(b"not really a png, the planner never opens it")
    row = VideoAsset(
        id=new_id("vasset"),
        project_id=project_id,
        kind=kind,
        path=str(path),
        sha256=new_id("sha"),
        width=1920,
        height=1080,
        duration_ms=duration_ms,
        has_audio=has_audio,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def make_item(
    session: Session,
    project_id: str,
    *,
    start_ms: int,
    end_ms: int,
    kind: str = "media",
    asset_id: str | None = None,
    gallery_json: str = "[]",
    fit: str = "cover",
    muted: bool = True,
    solo: bool = False,
    placement: str = "box",
) -> VideoItem:
    row = VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind=kind,
        start_ms=start_ms,
        end_ms=end_ms,
        z=0,
        fit=fit,
        motion="none",
        asset_id=asset_id,
        deck_id=None,
        slide_id=None,
        gallery_json=gallery_json,
        orphaned=False,
        muted=muted,
        solo=solo,
        placement=placement,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def project(session: Session, client: TestClient) -> str:
    version = seed_version(session, text="剧集标签很好用", sizes=[3, 3, 3])
    session.commit()
    return version.project_id


# --- one block, no cuts --------------------------------------------------


def test_a_single_block_covers_the_whole_film(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 5000), original=5000),
    )
    assert len(plan.segments) == 1
    assert plan.segments[0].kind == KIND_IMAGE
    assert plan.segments[0].duration_ms == 5000
    assert not plan.empty


# --- the block's own sound, in output time (§19.19) --------------------


def test_a_muted_block_contributes_no_sound(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(
        session, project, tmp_path, kind=KIND_VIDEO, duration_ms=9000, has_audio=True
    )
    item = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 5000), original=5000),
    )
    # Silent is the default, and it is what keeps the mux on its `-c copy` path.
    assert plan.audible == ()


def test_an_unmuted_block_contributes_its_own_stretch(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(
        session, project, tmp_path, kind=KIND_VIDEO, duration_ms=9000, has_audio=True
    )
    item = make_item(
        session, project, start_ms=1000, end_ms=4000, asset_id=asset.id, muted=False
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 5000), original=5000),
    )
    assert len(plan.audible) == 1
    assert (plan.audible[0].start_ms, plan.audible[0].end_ms) == (1000, 4000)


def test_the_sound_follows_the_picture_across_a_cut(
    session: Session, project: str, tmp_path: Path
) -> None:
    """The deletion of 1000 ms *before* the block shifts both, by the same 1000 ms.

    Unmuted sound that kept its original instant would start one second after its picture,
    and one more second for every further cut before it — a drift, not an offset.
    """
    asset = make_asset(
        session, project, tmp_path, kind=KIND_VIDEO, duration_ms=9000, has_audio=True
    )
    item = make_item(
        session, project, start_ms=3000, end_ms=5000, asset_id=asset.id, muted=False
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        # Dropped 1000–2000, so everything after 2000 is 1000 ms earlier in the film.
        edl=edl_of((0, 1000), (2000, 5000), original=5000),
    )
    assert len(plan.audible) == 1
    assert (plan.audible[0].start_ms, plan.audible[0].end_ms) == (2000, 4000)
    # And the picture agrees — that is the whole point. The plate fills output [0, 2000):
    # the surviving first second (0–1000) plus the second that was never cut out of the
    # block's own approach (2000–3000 maps to output 1000–2000).
    before_the_block = sum(
        segment.duration_ms for segment in plan.segments if segment.kind == KIND_PLATE
    )
    assert before_the_block == 2000


def test_a_block_whose_audio_is_gone_contributes_no_sound(
    session: Session, project: str, tmp_path: Path
) -> None:
    """An orphan has no picture; a sound under no picture is worse than silence."""
    asset = make_asset(
        session, project, tmp_path, kind=KIND_VIDEO, duration_ms=9000, has_audio=True
    )
    item = make_item(
        session, project, start_ms=1000, end_ms=2000, asset_id=asset.id, muted=False
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        # The block's whole span is deleted.
        edl=edl_of((0, 1000), (2000, 5000), original=5000),
    )
    assert plan.orphaned == 1
    assert plan.audible == ()


def test_a_still_is_never_audible(session: Session, project: str, tmp_path: Path) -> None:
    """Unmuting a PNG is a state, not a sound: `has_audio` is what decides."""
    asset = make_asset(session, project, tmp_path, kind=KIND_IMAGE)
    item = make_item(
        session, project, start_ms=0, end_ms=2000, asset_id=asset.id, muted=False
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 2000), original=2000),
    )
    assert plan.audible == ()


def test_a_gallery_speaks_once_with_its_first_sound(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Three images and one clip: the clip's sound covers the block, not a third of it.

    Restarting the sound at every member would produce a stutter, and the members are
    pictures changing under one continuous block.
    """
    still = make_asset(session, project, tmp_path, kind=KIND_IMAGE)
    clip = make_asset(
        session, project, tmp_path, kind=KIND_VIDEO, duration_ms=9000, has_audio=True
    )
    members = json.dumps([{"asset_id": still.id}, {"asset_id": clip.id}])
    item = make_item(
        session,
        project,
        start_ms=0,
        end_ms=6000,
        kind="gallery",
        gallery_json=members,
        muted=False,
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 6000), original=6000),
    )
    assert len(plan.audible) == 1
    assert (plan.audible[0].start_ms, plan.audible[0].end_ms) == (0, 6000)


def test_the_box_rect_is_carried_through(session: Session, project: str, tmp_path: Path) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=0, end_ms=1000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 1000), original=1000),
    )
    assert (plan.box_x, plan.box_y, plan.width, plan.height) == (240, 209, 1440, 810)


# --- a deletion inside the block ----------------------------------------


def test_a_deletion_shortens_the_block_without_moving_it(
    session: Session, project: str, tmp_path: Path
) -> None:
    """10 s block, 2 s cut out of the middle: it plays 8 s, starting where it started."""
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=0, end_ms=10_000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), (6000, 10_000), original=10_000),
    )
    assert [segment.duration_ms for segment in plan.segments] == [8000]


def test_a_block_after_a_cut_starts_later_in_the_film(
    session: Session, project: str, tmp_path: Path
) -> None:
    """The gap left by the deleted audio must not leave a hole in the chain."""
    asset = make_asset(session, project, tmp_path)
    first = make_item(session, project, start_ms=0, end_ms=2000, asset_id=asset.id)
    second = make_item(session, project, start_ms=4000, end_ms=6000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[first, second],
        # 2000–4000 is deleted, so the second block begins at 2000 in the film.
        edl=edl_of((0, 2000), (4000, 6000), original=6000),
    )
    assert plan.total_ms == 4000
    assert [segment.duration_ms for segment in plan.segments] == [2000, 2000]
    assert all(segment.kind != KIND_PLATE for segment in plan.segments)


# --- gaps ---------------------------------------------------------------


def test_a_gap_becomes_a_plate_excerpt(session: Session, project: str, tmp_path: Path) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=2000, end_ms=4000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 5000), original=5000),
    )
    kinds = [segment.kind for segment in plan.segments]
    assert kinds == [KIND_PLATE, KIND_IMAGE, KIND_PLATE]
    assert [segment.duration_ms for segment in plan.segments] == [2000, 2000, 1000]
    assert plan.total_ms == 5000


def test_a_plate_segment_crops_exactly_the_box_rect(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=1000, end_ms=2000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 3000), original=3000),
    )
    plates = [segment for segment in plan.segments if segment.kind == KIND_PLATE]
    for plate in plates:
        assert plate.crop == (240, 209, 1440, 810)


def test_the_chain_always_totals_the_output_length(
    session: Session, project: str, tmp_path: Path
) -> None:
    """One frame short and the last frame freezes; one frame long and everything shifts."""
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=1500, end_ms=4500, asset_id=asset.id)
    edl = edl_of((0, 7000), original=9000)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl,
    )
    assert plan.total_ms == edl.output_duration_ms


def test_no_blocks_at_all_still_produces_a_full_chain(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A film with an empty box is a legitimate export, not an error."""
    edl = edl_of((0, 3000), original=3000)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[],
        edl=edl,
    )
    assert plan.empty
    assert plan.total_ms == 3000
    assert all(segment.kind == KIND_PLATE for segment in plan.segments)


# --- orphans ------------------------------------------------------------


def test_a_block_whose_audio_is_gone_is_counted_not_rendered(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    kept = make_item(session, project, start_ms=0, end_ms=1000, asset_id=asset.id)
    doomed = make_item(session, project, start_ms=2000, end_ms=3000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[kept, doomed],
        # 2000–3000 deleted entirely: only the first block survives.
        edl=edl_of((0, 1000), original=3000),
    )
    assert plan.orphaned >= 1
    assert all(segment.kind != KIND_PLATE for segment in plan.segments[:1])


def test_a_block_with_no_asset_yet_contributes_nothing(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A block created before a file is chosen is a normal intermediate state."""
    item = make_item(session, project, start_ms=0, end_ms=1000)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 2000), original=2000),
    )
    assert plan.empty
    assert plan.total_ms == 2000


# --- galleries ----------------------------------------------------------


def test_a_gallery_expands_into_one_segment_per_image(
    session: Session, project: str, tmp_path: Path
) -> None:
    first = make_asset(session, project, tmp_path)
    second = make_asset(session, project, tmp_path)
    item = make_item(
        session,
        project,
        start_ms=0,
        end_ms=4000,
        kind="gallery",
        gallery_json=f'[{{"asset_id": "{first.id}"}}, {{"asset_id": "{second.id}"}}]',
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), original=4000),
    )
    assert len(plan.segments) == 2
    assert [segment.duration_ms for segment in plan.segments] == [2000, 2000]


def test_a_gallery_with_malformed_json_renders_nothing_rather_than_failing(
    session: Session, project: str, tmp_path: Path
) -> None:
    item = make_item(
        session, project, start_ms=0, end_ms=1000, kind="gallery", gallery_json="{not json"
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 1000), original=1000),
    )
    assert plan.empty


def test_a_gallery_skips_members_that_are_gone(
    session: Session, project: str, tmp_path: Path
) -> None:
    alive = make_asset(session, project, tmp_path)
    item = make_item(
        session,
        project,
        start_ms=0,
        end_ms=2000,
        kind="gallery",
        gallery_json=f'[{{"asset_id": "vasset_missing"}}, {{"asset_id": "{alive.id}"}}]',
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 2000), original=2000),
    )
    assert len(plan.segments) == 1
    assert plan.segments[0].duration_ms == 2000


def test_gallery_pacing_shrinks_after_a_cut(session: Session, project: str, tmp_path: Path) -> None:
    """The proportional rule (§19.3.1): shorter block, same number of turns."""
    first = make_asset(session, project, tmp_path)
    second = make_asset(session, project, tmp_path)
    item = make_item(
        session,
        project,
        start_ms=0,
        end_ms=8000,
        kind="gallery",
        gallery_json=f'[{{"asset_id": "{first.id}"}}, {{"asset_id": "{second.id}"}}]',
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        # 2 s of the 8 s are deleted → each image drops from 4 s to 3 s.
        edl=edl_of((0, 3000), (5000, 8000), original=8000),
    )
    assert len(plan.segments) == 2
    assert [segment.duration_ms for segment in plan.segments] == [3000, 3000]


# --- a video clip -------------------------------------------------------


def test_a_short_clip_is_marked_for_looping(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A 2 s clip in a 10 s block repeats; freezing on its last frame looks broken."""
    asset = make_asset(session, project, tmp_path, kind=KIND_VIDEO, duration_ms=2000)
    item = make_item(session, project, start_ms=0, end_ms=10_000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.segments[0].kind == KIND_VIDEO
    assert plan.segments[0].loop


def test_a_long_clip_is_not_looped(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path, kind=KIND_VIDEO, duration_ms=30_000)
    item = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 5000), original=5000),
    )
    assert not plan.segments[0].loop


# --- overlapping blocks -------------------------------------------------


def test_overlapping_blocks_are_trimmed_not_dropped(
    session: Session, project: str, tmp_path: Path
) -> None:
    """The chain must stay exactly as long as the film even if the user overlapped."""
    asset = make_asset(session, project, tmp_path)
    first = make_item(session, project, start_ms=0, end_ms=3000, asset_id=asset.id)
    second = make_item(session, project, start_ms=2000, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[first, second],
        edl=edl_of((0, 5000), original=5000),
    )
    assert plan.total_ms == 5000


# --- inputs -------------------------------------------------------------


def test_the_same_file_used_twice_gets_one_input(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A gallery image shown twice must not become two `-i` arguments."""
    asset = make_asset(session, project, tmp_path)
    item = make_item(
        session,
        project,
        start_ms=0,
        end_ms=4000,
        kind="gallery",
        gallery_json=f'[{{"asset_id": "{asset.id}"}}, {{"asset_id": "{asset.id}"}}]',
    )
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), original=4000),
    )
    assert len(plan.segments) == 2
    assert len(plan.sources) == 1


def test_two_gaps_share_the_plate_input(session: Session, project: str, tmp_path: Path) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=1000, end_ms=2000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), original=4000),
    )
    plates = [segment for segment in plan.segments if segment.kind == KIND_PLATE]
    assert len(plates) == 2
    assert plates[0].input_index == plates[1].input_index


# --- covering (the still preview) --------------------------------------


def test_covering_finds_the_segment_under_an_instant(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=1000, end_ms=3000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), original=4000),
    )
    covered = video_box.covering(plan, at_ms=1500)
    assert covered is not None
    segment, _source, offset = covered
    assert segment.kind == KIND_IMAGE
    assert offset == 500


def test_covering_of_a_gap_returns_the_plate(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=2000, end_ms=3000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 4000), original=4000),
    )
    covered = video_box.covering(plan, at_ms=500)
    assert covered is not None
    assert covered[0].kind == KIND_PLATE


def test_covering_past_the_end_returns_none(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    item = make_item(session, project, start_ms=0, end_ms=1000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 1000), original=1000),
    )
    assert video_box.covering(plan, at_ms=99_999) is None


def test_a_template_without_a_box_is_refused(
    session: Session, project: str, tmp_path: Path
) -> None:
    with pytest.raises(Exception) as caught:
        video_box.plan(
            session,
            template_background=tmp_path / "bg.png",
            box_rect={},
            items=[],
            edl=edl_of((0, 1000), original=1000),
        )
    assert "素材框" in str(caught.value)


# --- placement (round 111, §19.25) ---------------------------------------


def test_every_segment_a_block_emits_inherits_its_placement(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Including a gallery's later members, which are rebuilt in a loop.

    Losing the flag there would look like "the first image fills the screen and the second one
    jumps back into the box" — a bug with no error attached to it.
    """
    first = make_asset(session, project, tmp_path)
    second = make_asset(session, project, tmp_path)
    gallery = make_item(
        session,
        project,
        start_ms=0,
        end_ms=10_000,
        kind="gallery",
        gallery_json=json.dumps([{"asset_id": first.id}, {"asset_id": second.id}]),
        placement="full",
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[gallery],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert len(plan.segments) >= 2
    assert {segment.placement for segment in plan.segments} == {"full"}


def test_a_box_block_stays_put_when_its_neighbour_is_full(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    full = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id, placement="full")
    boxed = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, placement="box"
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full, boxed],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert [segment.placement for segment in plan.segments] == ["full", "box"]


def test_one_full_block_makes_the_plan_full_frame(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    full = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id, placement="full")
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full],
        edl=edl_of((0, 5000), original=5000),
    )
    assert plan.full_frame is True


def test_a_film_of_box_blocks_is_not_full_frame(
    session: Session, project: str, tmp_path: Path
) -> None:
    """This flag is what keeps every existing project on the chain it already renders with."""
    asset = make_asset(session, project, tmp_path)
    boxed = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[boxed],
        edl=edl_of((0, 5000), original=5000),
    )
    assert plan.full_frame is False


def test_a_gap_next_to_a_full_block_is_still_a_box_plate(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A gap is the plate seen through the box, regardless of what its neighbour does."""
    asset = make_asset(session, project, tmp_path)
    full = make_item(session, project, start_ms=4000, end_ms=8000, asset_id=asset.id, placement="full")
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full],
        edl=edl_of((0, 10_000), original=10_000),
    )
    plates = [segment for segment in plan.segments if segment.kind == KIND_PLATE]
    assert plates, "开头 4 秒应该是 plate"
    for segment in plates:
        assert segment.placement == "box"
        assert segment.crop == (BOX["x"], BOX["y"], BOX["w"], BOX["h"])


# --- 「S」 rides on the plan too -------------------------------------------


def test_the_plan_records_which_stretches_replace_the_podcast(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path, kind=KIND_VIDEO, duration_ms=5000, has_audio=True)
    solo = make_item(
        session, project, start_ms=0, end_ms=5000, asset_id=asset.id, muted=False, solo=True
    )
    mixed = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, muted=False, solo=False
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[solo, mixed],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert [(item.start_ms, item.solo) for item in plan.audible] == [(0, True), (5000, False)]


def test_a_muted_block_is_never_audible_even_when_soloed(
    session: Session, project: str, tmp_path: Path
) -> None:
    """The server keeps M and S exclusive, but the plan must not depend on that being true."""
    asset = make_asset(session, project, tmp_path, kind=KIND_VIDEO, duration_ms=5000, has_audio=True)
    row = make_item(
        session, project, start_ms=0, end_ms=5000, asset_id=asset.id, muted=True, solo=True
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[row],
        edl=edl_of((0, 5000), original=5000),
    )
    assert plan.audible == ()


# --- 两条链：框内在下，全屏在上 (§19.26) --------------------------------


def test_a_plan_without_a_full_block_has_no_split(
    session: Session, project: str, tmp_path: Path
) -> None:
    """`split is None` is what keeps every existing project on the single opaque chain."""
    asset = make_asset(session, project, tmp_path)
    boxed = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[boxed],
        edl=edl_of((0, 5000), original=5000),
    )
    assert plan.split is None


def test_the_two_halves_have_the_same_boundaries(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Segment N is the same instant in both chains, or the two overlays drift apart."""
    asset = make_asset(session, project, tmp_path)
    full = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id, placement="full")
    boxed = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, placement="box"
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full, boxed],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.split is not None
    lower = [(segment.duration_ms, segment.placement) for segment in plan.split.lower]
    upper = [(segment.duration_ms, segment.placement) for segment in plan.split.upper]
    assert [duration for duration, _ in lower] == [duration for duration, _ in upper]
    assert sum(duration for duration, _ in lower) == 10_000


def test_a_box_block_is_drawn_by_the_lower_chain_only(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_asset(session, project, tmp_path)
    boxed = make_item(session, project, start_ms=0, end_ms=5000, asset_id=asset.id)
    full = make_item(session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, placement="full")
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[boxed, full],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.split is not None
    assert [segment.kind for segment in plan.split.lower] == [
        KIND_IMAGE,
        KIND_TRANSPARENT,
    ]
    assert [segment.kind for segment in plan.split.upper] == [
        KIND_TRANSPARENT,
        KIND_IMAGE,
    ]


def test_each_half_declares_only_its_own_inputs(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Per-half input lists are what stop the upper chain having to count the lower one's."""
    first = make_asset(session, project, tmp_path)
    second = make_asset(session, project, tmp_path)
    boxed = make_item(session, project, start_ms=0, end_ms=5000, asset_id=first.id)
    full = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=second.id, placement="full"
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[boxed, full],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.split is not None
    assert list(plan.split.lower_sources) == [Path(first.path)]
    assert list(plan.split.upper_sources) == [Path(second.path)]
    # …and the indices point into their own list, not into the plan's.
    assert plan.split.lower[0].input_index == 0
    assert plan.split.upper[1].input_index == 0


def test_a_gap_is_transparent_in_both_halves(
    session: Session, project: str, tmp_path: Path
) -> None:
    """A gap draws the plate through the box in the lower chain and nothing on top."""
    asset = make_asset(session, project, tmp_path)
    full = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, placement="full"
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.split is not None
    # The leading gap is a plate excerpt for the lower chain and a placeholder for the top one.
    assert plan.split.lower[0].kind == KIND_PLATE
    assert plan.split.upper[0].kind == KIND_TRANSPARENT


def test_a_placeholder_is_not_an_input(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Placeholders are `color` filters. Counting one as an input shifts every later layer."""
    asset = make_asset(session, project, tmp_path)
    full = make_item(
        session, project, start_ms=5000, end_ms=10_000, asset_id=asset.id, placement="full"
    )
    plan = video_box.plan(
        session,
        template_background=Path("plate.png"),
        box_rect=BOX,
        items=[full],
        edl=edl_of((0, 10_000), original=10_000),
    )
    assert plan.split is not None
    lower = build_canvas_chain(
        list(plan.split.lower),
        canvas_w=1920,
        canvas_h=1080,
        box_w=BOX["w"],
        box_h=BOX["h"],
    )
    # plate + placeholder → still exactly one real input (the plate)
    assert lower.inputs == 1
