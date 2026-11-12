"""Round 104: a slide block opens on the page it starts inside; blocks carry a name.

Both defects produced a *plausible* screen rather than an error, which is why they lasted:

* **The wrong page.** The planner kept only pages whose ``start_ms`` fell inside the block.
  A block at 01:10 whose page began at 00:05 therefore had no page inside it and rendered the
  *next* page — the user pressed 「放进素材框」 on 开场与节目定位 and the still showed
  嘉宾介绍.
* **The wrong name.** A block was labelled by ``kind`` alone and a library row by the hash
  baked into its stored file name, so two imported stills were indistinguishable.

Pinned here as facts about the plan and the timeline payload. No ffmpeg: the plan is
arithmetic and the labels are a read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.edl import Deletion, Edl, Keep
from animatter.ids import new_id, utcnow_iso
from animatter.media.box_track import KIND_IMAGE
from animatter.models import Deck, Slide, VideoAsset, VideoItem
from animatter.services import video as video_service
from animatter.services import video_box

BOX = {"x": 240, "y": 209, "w": 1440, "h": 810}


def edl_of(*keeps: tuple[int, int], original: int) -> Edl:
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


def make_deck(
    session: Session, project_id: str, *, title: str, pages: list[tuple[str, int]]
) -> Deck:
    deck = Deck(
        id=new_id("deck"),
        project_id=project_id,
        title=title,
        source="model",
        warnings_json="[]",
        title_font="",
        body_font="",
        created_at=utcnow_iso(),
    )
    session.add(deck)
    for ord_, (page_title, start_ms) in enumerate(pages):
        session.add(
            Slide(
                id=new_id("sld"),
                deck_id=deck.id,
                ord=ord_,
                title=page_title,
                body="正文",
                layout="title_body",
                source_ord=ord_,
                start_ms=start_ms,
            )
        )
    session.flush()
    return deck


def slide_tables(
    session: Session, deck: Deck
) -> tuple[dict[str, Path], dict[str, list[tuple[int, str]]]]:
    """``slide_images`` / ``slide_starts`` in the shape the planner takes.

    The image paths are named after the page, so an assertion about which page played reads
    as the page's title rather than as an opaque id.
    """
    rows = sorted(
        session.query(Slide).filter(Slide.deck_id == deck.id), key=lambda row: row.ord
    )
    images = {row.id: Path(f"{row.title}.png") for row in rows}
    starts = {deck.id: [(row.start_ms, row.id) for row in rows]}
    return images, starts


def make_slide_item(
    session: Session, project_id: str, *, deck_id: str, start_ms: int, end_ms: int
) -> VideoItem:
    row = VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind="slide",
        start_ms=start_ms,
        end_ms=end_ms,
        z=0,
        fit="contain",
        motion="none",
        asset_id=None,
        deck_id=deck_id,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def make_image(
    session: Session, project_id: str, tmp_path: Path, *, name: str
) -> VideoAsset:
    path = tmp_path / f"{new_id('f')}.png"
    path.write_bytes(b"not really a png, the planner never opens it")
    row = VideoAsset(
        id=new_id("vasset"),
        project_id=project_id,
        kind="image",
        path=str(path),
        original_name=name,
        sha256=new_id("sha"),
        width=1920,
        height=1080,
        duration_ms=0,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def project(session: Session, client: TestClient) -> str:
    version = seed_version(session, text="剧集标签很好用", sizes=[3, 3, 3])
    session.commit()
    return version.project_id


# --- which page a block opens on ---------------------------------------


def test_a_block_starting_inside_a_page_shows_that_page(
    session: Session, project: str
) -> None:
    """The round-104 report as arithmetic.

    Pages at 5.1 s and 132.98 s; the block starts at 70.97 s, inside 开场与节目定位, which
    began 65.87 s earlier. The block's first stretch must still be that page.
    """
    deck = make_deck(
        session,
        project,
        title="演示稿",
        pages=[("开场与节目定位", 5100), ("嘉宾介绍", 132_980)],
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=70_970, end_ms=198_850)
    images, starts = slide_tables(session, deck)

    stretches = video_box._slide_stretches(
        item,
        item.end_ms - item.start_ms,
        time_map=None,
        slide_images=images,
        slide_starts=starts,
    )

    assert [path.stem for _segment, path in stretches] == ["开场与节目定位", "嘉宾介绍"]
    # 70 970 → 132 980 is the tail of page 1; 132 980 → 198 850 is page 2.
    assert [segment.duration_ms for segment, _path in stretches] == [62_010, 65_870]
    assert sum(segment.duration_ms for segment, _path in stretches) == 127_880


def test_a_block_starting_on_a_page_boundary_shows_that_page(
    session: Session, project: str
) -> None:
    """Half-open ranges: the page starting exactly at the block's start owns it."""
    deck = make_deck(
        session, project, title="演示稿", pages=[("第一节", 1000), ("第二节", 5000)]
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=5000, end_ms=9000)
    images, starts = slide_tables(session, deck)

    stretches = video_box._slide_stretches(
        item,
        item.end_ms - item.start_ms,
        time_map=None,
        slide_images=images,
        slide_starts=starts,
    )

    assert [path.stem for _segment, path in stretches] == ["第二节"]


def test_a_block_that_ends_inside_a_page_stops_at_its_own_end(
    session: Session, project: str
) -> None:
    """The last page gets the block's end, not the next page's start."""
    deck = make_deck(
        session, project, title="演示稿", pages=[("第一节", 0), ("第二节", 60_000)]
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=10_000, end_ms=30_000)
    images, starts = slide_tables(session, deck)

    stretches = video_box._slide_stretches(
        item,
        item.end_ms - item.start_ms,
        time_map=None,
        slide_images=images,
        slide_starts=starts,
    )

    assert [path.stem for _segment, path in stretches] == ["第一节"]
    assert stretches[0][0].duration_ms == 20_000


def test_a_block_before_the_first_page_starts_the_deck_where_it_says(
    session: Session, project: str
) -> None:
    """A block opening before page 1 has nothing to show yet, so page 1 is the opening.

    The block covers ``[0, 20 000)`` and the deck's first page starts at 5 000; the head is
    filled with page 1 rather than a flash of plate — its span is what the user drew.
    """
    deck = make_deck(
        session, project, title="演示稿", pages=[("第一节", 5000), ("第二节", 60_000)]
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=0, end_ms=20_000)
    images, starts = slide_tables(session, deck)

    stretches = video_box._slide_stretches(
        item,
        item.end_ms - item.start_ms,
        time_map=None,
        slide_images=images,
        slide_starts=starts,
    )

    assert [path.stem for _segment, path in stretches] == ["第一节"]
    assert stretches[0][0].duration_ms == 20_000


def test_a_page_that_was_cut_out_of_the_audio_does_not_take_the_block_with_it(
    session: Session, project: str
) -> None:
    """``map_range`` returning ``None`` drops that page's stretch, not the whole block."""
    deck = make_deck(
        session,
        project,
        title="演示稿",
        pages=[("开场与节目定位", 5100), ("嘉宾介绍", 132_980)],
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=70_970, end_ms=198_850)
    images, starts = slide_tables(session, deck)
    # 70 970 → 132 980 deleted: only page 2 survives.
    edl = edl_of((0, 70_970), (132_980, 198_850), original=198_850)

    stretches = video_box._slide_stretches(
        item,
        65_870,
        time_map=edl.time_map(),
        slide_images=images,
        slide_starts=starts,
    )

    assert [path.stem for _segment, path in stretches] == ["嘉宾介绍"]


def test_the_plan_opens_on_the_page_covering_the_block_start(
    session: Session, project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule one level up, through ``plan()`` — the path 「生成静帧」 and the film take.

    ``_slide_tables`` is stubbed so the test does not have to draw 30 PNGs: what is under
    test is the ordering decision, not Pillow.
    """
    deck = make_deck(
        session,
        project,
        title="演示稿",
        pages=[("开场与节目定位", 5100), ("嘉宾介绍", 132_980)],
    )
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=70_970, end_ms=198_850)
    images, starts = slide_tables(session, deck)
    monkeypatch.setattr(
        video_box, "_slide_tables", lambda *_args, **_kwargs: (images, starts)
    )

    plan = video_box.plan(
        session,
        template_background=tmp_path / "bg.png",
        box_rect=BOX,
        items=[item],
        edl=edl_of((0, 198_850), original=198_850),
    )

    # The plate is an input as well (the gap before the block is a plate excerpt), so the
    # pages are read through the segments rather than straight off `sources`.
    page_segments = [segment for segment in plan.segments if segment.kind == KIND_IMAGE]
    assert [plan.sources[segment.input_index].stem for segment in page_segments] == [
        "开场与节目定位",
        "嘉宾介绍",
    ]
    assert [segment.duration_ms for segment in page_segments] == [62_010, 65_870]
    assert plan.total_ms == 198_850


# --- what a block is called --------------------------------------------


def _labels(session: Session, project: str) -> dict[str, str]:
    return video_service._labels_for(session, video_service.list_items(session, project))


def test_a_media_block_is_named_after_the_imported_file(
    session: Session, project: str, tmp_path: Path
) -> None:
    asset = make_image(session, project, tmp_path, name="开场封面.png")
    item = VideoItem(
        id=new_id("vitem"),
        project_id=project,
        kind="media",
        start_ms=0,
        end_ms=5000,
        z=0,
        fit="cover",
        motion="none",
        asset_id=asset.id,
        deck_id=None,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(item)
    session.flush()

    assert _labels(session, project)[item.id] == "开场封面.png"


def test_a_slide_block_is_named_after_the_deck(
    session: Session, project: str
) -> None:
    deck = make_deck(session, project, title="费那奇 MV 访谈", pages=[("开场", 0)])
    item = make_slide_item(session, project, deck_id=deck.id, start_ms=0, end_ms=5000)
    assert _labels(session, project)[item.id] == "费那奇 MV 访谈"


def test_a_gallery_is_named_after_its_members(
    session: Session, project: str, tmp_path: Path
) -> None:
    first = make_image(session, project, tmp_path, name="海报.png")
    second = make_image(session, project, tmp_path, name="剧照.png")
    session.add(
        VideoItem(
            id=new_id("vitem"),
            project_id=project,
            kind="gallery",
            start_ms=0,
            end_ms=8000,
            z=0,
            fit="cover",
            motion="none",
            asset_id=None,
            deck_id=None,
            slide_id=None,
            gallery_json=f'[{{"asset_id": "{first.id}"}}, {{"asset_id": "{second.id}"}}]',
            orphaned=False,
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
    )
    session.flush()

    labels = _labels(session, project)
    assert list(labels.values()) == ["海报.png 等 2 张"]


def test_an_unnamed_block_falls_back_to_the_stored_file_name(
    session: Session, project: str, tmp_path: Path
) -> None:
    """Rows imported before the column existed have ``original_name = ""``."""
    asset = make_image(session, project, tmp_path, name="")
    item = VideoItem(
        id=new_id("vitem"),
        project_id=project,
        kind="media",
        start_ms=0,
        end_ms=5000,
        z=0,
        fit="cover",
        motion="none",
        asset_id=asset.id,
        deck_id=None,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(item)
    session.flush()

    label = _labels(session, project)[item.id]
    assert label == Path(asset.path).name
    assert label.endswith(".png")


def test_the_timeline_payload_carries_the_label(client: TestClient, session: Session) -> None:
    """The wire, not just the service: the track reads ``items[].label``."""
    version = seed_version(session, text="剧集标签很好用", sizes=[3, 3, 3])
    session.commit()
    deck = make_deck(session, version.project_id, title="演示稿", pages=[("开场", 0)])
    # Committed before the request: the client writes through its own connection, and an
    # open write transaction here would lock the file (the failure reads as a mystery
    # `database is locked` rather than as a missing commit).
    session.commit()
    client.post(
        f"/api/versions/{version.id}/video/items",
        json={
            "kind": "slide",
            "start_ms": 0,
            "end_ms": 1000,
            "deck_id": deck.id,
            "snap": False,
        },
    )
    body = client.get(f"/api/versions/{version.id}/video/timeline").json()
    assert body["items"][0]["label"] == "演示稿"


