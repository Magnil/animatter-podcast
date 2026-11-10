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

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.edl import Deletion, Edl, Keep
from animatter.ids import new_id, utcnow_iso
from animatter.media.box_track import KIND_IMAGE, KIND_PLATE, KIND_VIDEO
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
