"""Turning timeline blocks into the box content chain (§19.5).

The mapping this module performs is the crux of "video follows audio":

    original timeline   [block.start_ms, block.end_ms)     ← what the user dragged
    output timeline     [start, start + output_ms)         ← what the film plays

Blocks live on the *original* clock and snap to sentence starts; the film is shorter
wherever audio was cut. ``TimeMap.map_range`` bridges the two, and a block whose audio was
deleted entirely maps to ``None`` — an orphan, which contributes no segment at all (its row
stays; §19.3 explains why).

**Gaps are filled with plate excerpts.** Blocks need not tile the timeline, but an
``overlay`` input must span the whole film. A gap therefore becomes a segment cropped out
of the compiled background at the box rect: over a gap the overlay draws the plate
pixel-for-pixel over the plate, which is exactly what "no content here" should look like.
Without this, a film whose first block starts at 5 s would show that block from 0 s onward
and everything after it would be shifted — a drift that looks like a broken clock.

**Galleries expand here, not in the renderer.** A gallery is one timeline block whose
*content* changes several times; ``core/video_box.split_gallery`` produces those stretches
from cumulative weights, so the arithmetic that decides "each image gets 1.9 s" stays
testable without ffmpeg.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.core.edl import Edl, TimeMap
from animatter.core.video_box import split_gallery
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import box_track
from animatter.models import Slide, VideoAsset, VideoItem
from animatter.services import decks as slide_service
from animatter.services import video_assets as asset_service

log = get_logger(__name__)


@dataclass(frozen=True)
class PlannedBlock:
    """One timeline block already resolved onto the output timeline."""

    item: VideoItem
    start_ms: int
    end_ms: int
    pieces: list[tuple[box_track.BoxSegment, Path]]


@dataclass(frozen=True)
class BoxPlan:
    """The whole chain plus what the renderer needs to place it."""

    segments: list[box_track.BoxSegment]
    sources: list[Path]
    width: int
    height: int
    box_x: int
    box_y: int
    #: Blocks dropped because their audio no longer exists. Reported, never silent.
    orphaned: int = 0
    #: True when every segment is a plate excerpt: the film genuinely has no box content.
    empty: bool = True

    @property
    def total_ms(self) -> int:
        return sum(segment.duration_ms for segment in self.segments)

    @property
    def input_count(self) -> int:
        return len(self.sources)


def _gallery_ids(raw: str) -> list[tuple[str, int]]:
    """Parse ``[{"asset_id": …, "weight": …}]``, tolerating anything malformed.

    Malformed entries are skipped rather than raising: ``gallery_json`` is user-editable
    intermediate state (a drag can land mid-edit), and refusing to render the whole film
    because one entry lost its id turns a cosmetic problem into a failed export.
    """
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[tuple[str, int]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        asset_id = entry.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        try:
            weight = int(entry.get("weight") or 1)
        except (TypeError, ValueError):
            weight = 1
        out.append((asset_id, max(1, weight)))
    return out


def _asset(
    session: Session, cache: dict[str, VideoAsset | None], asset_id: str
) -> VideoAsset | None:
    if asset_id not in cache:
        cache[asset_id] = asset_service.get(session, asset_id)
    return cache[asset_id]


def _stretches_for(
    session: Session,
    item: VideoItem,
    cache: dict[str, VideoAsset | None],
    output_ms: int,
    *,
    span_ms: int = 0,
    time_map: TimeMap | None = None,
    slide_images: dict[str, Path] | None = None,
    slide_starts: dict[str, list[tuple[int, str]]] | None = None,
) -> list[tuple[box_track.BoxSegment, Path]]:
    """Every (segment, source file) pair one block contributes.

    ``input_index`` is filled in later by :func:`plan`, which owns the deduplicated list of
    inputs — one file used twice still gets one ffmpeg input.

    A ``slide`` block is the one kind that needs the **original** time map: its pages carry
    original-timeline millisecond starts (``slide.start_ms``), and which page is on screen
    depends on where the playhead is in the *source*, not in the output. Mapping each page
    start through ``time_map`` is what keeps 「页切换点与字幕段落对齐」 true after audio is
    cut: a page whose opening sentence was deleted has no output position and is dropped,
    rather than appearing at the wrong moment.
    """
    fit = item.fit or "blur_fill"

    if item.kind == "slide":
        return _slide_stretches(
            item,
            output_ms,
            time_map=time_map,
            slide_images=slide_images or {},
            slide_starts=slide_starts or {},
        )

    if item.kind == "gallery":
        members = _gallery_ids(item.gallery_json)
        playable: list[tuple[VideoAsset, int]] = []
        for asset_id, weight in members:
            row = _asset(session, cache, asset_id)
            if row is None:
                continue
            playable.append((row, weight))
        if not playable:
            return []
        pieces = split_gallery(
            [weight for _row, weight in playable], start_ms=0, output_ms=output_ms
        )
        out: list[tuple[box_track.BoxSegment, Path]] = []
        for piece in pieces:
            row = playable[piece.index][0]
            out.append(
                (
                    box_track.BoxSegment(
                        input_index=0,
                        duration_ms=piece.duration_ms,
                        kind=row.kind,
                        fit=fit,
                        # An image has no intrinsic length; a clip shorter than its
                        # stretch repeats rather than freezing on its last frame.
                        loop=row.kind == box_track.KIND_VIDEO
                        and row.duration_ms > 0
                        and row.duration_ms < piece.duration_ms,
                    ),
                    Path(row.path),
                )
            )
        return out

    if item.asset_id:
        row = _asset(session, cache, item.asset_id)
        if row is None:
            return []
        return [
            (
                box_track.BoxSegment(
                    input_index=0,
                    duration_ms=output_ms,
                    kind=row.kind,
                    fit=fit,
                    loop=row.kind == box_track.KIND_VIDEO
                    and row.duration_ms > 0
                    and row.duration_ms < output_ms,
                ),
                Path(row.path),
            )
        ]

    # A block with neither an asset nor gallery members: the user created it but has not
    # picked anything yet. Contributes nothing rather than raising — that is a normal
    # intermediate state while dragging.
    del span_ms
    return []


def _slide_stretches(
    item: VideoItem,
    output_ms: int,
    *,
    time_map: TimeMap | None,
    slide_images: dict[str, Path],
    slide_starts: dict[str, list[tuple[int, str]]],
) -> list[tuple[box_track.BoxSegment, Path]]:
    """Which slide is on screen for how long, inside one block's stretch.

    The pages are shown in order and each runs until the next one, so the *durations* come
    from the gaps between page starts rather than from anything stored per page — inserting
    a page therefore does not require rewriting its neighbour (same reasoning as
    ``core/deck.page_spans``).
    """
    pages = sorted(slide_starts.get(item.deck_id or "", []))
    if not pages:
        return []

    inside = [
        (start, slide_id)
        for start, slide_id in pages
        if item.start_ms <= start < item.end_ms and slide_id in slide_images
    ]
    if not inside:
        return []

    out: list[tuple[box_track.BoxSegment, Path]] = []
    for index, (start_ms, slide_id) in enumerate(inside):
        next_start = inside[index + 1][0] if index + 1 < len(inside) else item.end_ms
        if time_map is not None:
            mapped = time_map.map_range(start_ms, next_start)
            if mapped is None or mapped[1] <= mapped[0]:
                continue
            duration = mapped[1] - mapped[0]
        else:
            duration = next_start - start_ms
        if duration <= 0:
            continue
        out.append(
            (
                box_track.BoxSegment(
                    input_index=0,
                    # A page is a still, so it has no intrinsic length; it fills its stretch.
                    duration_ms=duration,
                    kind=box_track.KIND_IMAGE,
                    fit=item.fit or "contain",
                    loop=False,
                ),
                slide_images[slide_id],
            )
        )

    # The block may start before its first page does (the user dragged the leading edge onto
    # an earlier sentence). Filling that head with the first page is better than a flash of
    # plate, and it is what the block's own span promises the viewer.
    if out:
        missing = output_ms - sum(segment.duration_ms for segment, _path in out)
        if missing > 0:
            first_segment, first_path = out[0]
            out[0] = (
                box_track.BoxSegment(
                    input_index=0,
                    duration_ms=first_segment.duration_ms + missing,
                    kind=first_segment.kind,
                    fit=first_segment.fit,
                    loop=False,
                ),
                first_path,
            )
    return out


def plan(
    session: Session,
    *,
    template_background: Path,
    box_rect: dict[str, int],
    items: list[VideoItem],
    edl: Edl,
    settings: object | None = None,
    project_id: str = "",
) -> BoxPlan:
    """Build the full chain for one export, in output time.

    ``template_background`` is the compiled plate: gap and orphan stretches are cut from
    it, which is also why this needs no separate "empty box" image.

    ``settings``/``project_id`` are only used to draw slide decks. Passing neither leaves a
    ``slide`` block contributing nothing, which is what the tests want when they are checking
    the pure geometry of the plan.
    """
    width = int(box_rect.get("w") or 0)
    height = int(box_rect.get("h") or 0)
    box_x = int(box_rect.get("x") or 0)
    box_y = int(box_rect.get("y") or 0)
    if width <= 0 or height <= 0:
        raise AppError("template_no_box", "模板没有可用的素材框", status=409)

    time_map = edl.time_map()
    cache: dict[str, VideoAsset | None] = {}
    planned: list[PlannedBlock] = []
    orphaned = 0

    # Slide decks are resolved once for the whole plan: the same deck can be used by several
    # blocks, and the PNGs must not be re-rendered per block.
    slide_images, slide_starts = _slide_tables(
        session, items, settings=settings, project_id=project_id, width=width, height=height
    )

    for item in items:
        mapped = time_map.map_range(item.start_ms, item.end_ms)
        if mapped is None or mapped[1] <= mapped[0]:
            # Its audio was deleted in full. The row survives so the user can restore the
            # sentence and get the block back, but it contributes nothing to this film.
            orphaned += 1
            continue
        start, end = mapped
        pieces = _stretches_for(
            session,
            item,
            cache,
            end - start,
            span_ms=item.end_ms - item.start_ms,
            time_map=time_map,
            slide_images=slide_images,
            slide_starts=slide_starts,
        )
        if not pieces:
            orphaned += 1
            continue
        planned.append(PlannedBlock(item=item, start_ms=start, end_ms=end, pieces=pieces))

    planned.sort(key=lambda block: block.start_ms)

    # Lay the blocks out, clipping overlaps and filling gaps with plate excerpts. The chain
    # must total exactly the output length or the overlay stops early and the frame freezes.
    placed: list[tuple[box_track.BoxSegment, Path]] = []
    cursor = 0
    for block in planned:
        if block.start_ms > cursor:
            placed.append(_plate(cursor, block.start_ms, box_x, box_y, width, height, template_background))
            cursor = block.start_ms
        overlap = cursor - block.start_ms
        pieces = _trim_start(block.pieces, overlap) if overlap > 0 else block.pieces
        for segment, source in pieces:
            placed.append((segment, source))
            cursor += segment.duration_ms

    if edl.output_duration_ms > cursor:
        placed.append(
            _plate(
                cursor,
                edl.output_duration_ms,
                box_x,
                box_y,
                width,
                height,
                template_background,
            )
        )

    # Deduplicate inputs by resolved path: a gallery image shown twice, or the plate used
    # for three gaps, must not become three separate `-i` arguments.
    sources: list[Path] = []
    by_path: dict[str, int] = {}
    final: list[box_track.BoxSegment] = []
    for segment, source in placed:
        key = str(source)
        if key not in by_path:
            by_path[key] = len(sources)
            sources.append(source)
        final.append(
            box_track.BoxSegment(
                input_index=by_path[key],
                duration_ms=segment.duration_ms,
                kind=segment.kind,
                fit=segment.fit,
                crop=segment.crop,
                loop=segment.loop,
            )
        )

    empty = not any(segment.kind != box_track.KIND_PLATE for segment in final)
    log.info(
        "box.planned",
        segments=len(final),
        inputs=len(sources),
        blocks=len(planned),
        orphaned=orphaned,
        empty=empty,
        total_ms=sum(segment.duration_ms for segment in final),
        output_ms=edl.output_duration_ms,
    )
    return BoxPlan(
        segments=final,
        sources=sources,
        width=width,
        height=height,
        box_x=box_x,
        box_y=box_y,
        orphaned=orphaned,
        empty=empty,
    )


def _slide_tables(
    session: Session,
    items: list[VideoItem],
    *,
    settings: object | None,
    project_id: str,
    width: int,
    height: int,
) -> tuple[dict[str, Path], dict[str, list[tuple[int, str]]]]:
    """Render every deck a plan needs, once, and index its pages by start time.

    The PNGs are drawn at the **media box's** size so they are never rescaled on the way in;
    a slide rendered at some other size would be resampled by the overlay and the text would
    go soft, which is the one thing a slide must not do.

    ``settings`` may be ``None`` when a caller only needs the plan's geometry (the tests do):
    the page starts are still returned, and no image is rendered. That keeps the pure part of
    the plan testable without a clock, a font or a filesystem.
    """
    deck_ids = {item.deck_id for item in items if item.kind == "slide" and item.deck_id}
    if not deck_ids:
        return {}, {}

    images: dict[str, Path] = {}
    starts: dict[str, list[tuple[int, str]]] = {}
    for deck_id in deck_ids:
        rows = session.scalars(
            select(Slide).where(Slide.deck_id == deck_id).order_by(Slide.ord)
        ).all()
        starts[deck_id] = [(row.start_ms, row.id) for row in rows]
        if settings is None:
            continue
        for row in rows:
            images[row.id] = slide_service.render_slide(
                settings,
                project_id,
                deck_id,
                row,
                width=width,
                height=height,
            )
    return images, starts


def _plate(
    start_ms: int, end_ms: int, x: int, y: int, width: int, height: int, background: Path
) -> tuple[box_track.BoxSegment, Path]:
    """A stretch that draws the plate over itself, so the overlay stays opaque."""
    return (
        box_track.BoxSegment(
            input_index=0,
            duration_ms=end_ms - start_ms,
            kind=box_track.KIND_PLATE,
            crop=(x, y, width, height),
        ),
        background,
    )


def _trim_start(
    pieces: list[tuple[box_track.BoxSegment, Path]], overlap_ms: int
) -> list[tuple[box_track.BoxSegment, Path]]:
    """Drop ``overlap_ms`` from the front of a block's stretches."""
    trimmed: list[tuple[box_track.BoxSegment, Path]] = []
    remaining = overlap_ms
    for segment, source in pieces:
        if remaining >= segment.duration_ms:
            remaining -= segment.duration_ms
            continue
        trimmed.append(
            (
                box_track.BoxSegment(
                    input_index=segment.input_index,
                    duration_ms=segment.duration_ms - remaining,
                    kind=segment.kind,
                    fit=segment.fit,
                    crop=segment.crop,
                    loop=segment.loop,
                ),
                source,
            )
        )
        remaining = 0
    return trimmed


def covering(
    plan_: BoxPlan, *, at_ms: int
) -> tuple[box_track.BoxSegment, Path, int] | None:
    """The segment that covers ``at_ms`` in output time, plus its offset inside itself.

    Used by the still preview, which must not build the whole film's chain to draw one
    frame.
    """
    path_of = {index: source for index, source in enumerate(plan_.sources)}
    cursor = 0
    for segment in plan_.segments:
        if cursor <= at_ms < cursor + segment.duration_ms:
            source = path_of.get(segment.input_index)
            if source is None:
                return None
            return segment, source, at_ms - cursor
        cursor += segment.duration_ms
    return None
