"""P1 routes: the DOM preview's plan, its lazily baked pixels, and the cue bake (§19.31.2).

**Why a plan instead of more preview requests.** A still costs 0.63–0.93 s because ffmpeg has to
start and composite. A plan is one request for the whole film — segments, avatar intervals and
cue geometry, 167 KB — refetched only when the project changes. After that the browser moves the
playhead with no network at all: two binary searches (§19.31.2).

**Why the baked pixels are a separate route.** Three of the artefacts a plan names are produced
*because* it exists: blurred backdrops, cue bands, and the text overlay when it had to be redrawn.
The rest (compiled template PNGs, timeline assets, deck pages) already have routes that validate
project ownership, so those are reused and only these three are served here.

The route takes a **file name**, never a path, and resolves it inside one directory. A route
that accepted a path would be a file-read primitive — the same guard `template_service.asset_path`
exists for, and the same reason this one is a name plus an allow-listed shape rather than
`Path(name).name`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import backdrop as backdrop_media
from animatter.media import cue_png
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.services import clipping as clipping_service
from animatter.services import video_plan

router = APIRouter(prefix="/api", tags=["video-plan"])
log = get_logger(__name__)

#: The **only** names this route will serve — spelled out per artefact shape, not as a general
#: "safe characters" filter. `Path(name).name` would already defeat `../..`, but a loose filter
#: would also accept any *future* file that happens to land in the bake directory, which is how
#: a narrow route quietly becomes a general one. A digest of the wrong length is not a name this
#: module can have produced, so it is refused rather than looked up.
_SAFE_NAME = re.compile(
    r"^(?:"
    # `video_plan.backdrop_name`: digest(16) + position(3) + fit
    r"backdrop-[0-9a-f]{16}-\d{3}-(?:blur_fill|blur_crop)\.jpg"
    # `video_plan.cue_name`: digest(16) + cue index(5)
    r"|cue-[0-9a-f]{16}-\d{5}\.png"
    # `video_render.text_png_for`: edit signature + visible-slot count + name-plate hash
    r"|text-[0-9a-f]{8,16}-[\d-]+-[0-9a-f]{8}\.png"
    r")$"
)


class _RectOut(BaseModel):
    x: int
    y: int
    w: int
    h: int


class _CanvasOut(BaseModel):
    w: int
    h: int


class _IntervalOut(BaseModel):
    start_ms: int
    end_ms: int


class _SegmentOut(BaseModel):
    start_ms: int
    end_ms: int
    kind: str
    fit: str
    placement: str
    loop: bool
    url: str
    backdrop_url: str | None = None
    backdrop_frozen: bool = False
    #: The period a browser's own `loop` repeats on, in ms. **Not** `element.duration`: ffmpeg's
    #: `-stream_loop` wraps at the stream duration while the element reports the container's, and
    #: the two differ by 32–83 ms on the sample — whole frames after a few loops. Zero when the
    #: segment does not loop.
    period_ms: int = 0


class _CueOut(BaseModel):
    start_ms: int
    end_ms: int
    index: int
    top: int
    height: int
    url: str


class _AvatarOut(BaseModel):
    name: str
    rect: _RectOut
    lit_url: str
    dim_url: str
    intervals: list[_IntervalOut]


class VideoPlanOut(BaseModel):
    digest: str
    version_id: str
    canvas: _CanvasOut
    box: _RectOut
    output_duration_ms: int
    background_url: str
    #: The media-box plate on its own, and where it goes. ``None`` for a template compiled
    #: before round 137, whose poster still has the plate baked in (§19.35).
    plate_url: str | None
    plate_rect: _RectOut | None
    #: The frame stroke, cropped to ``stroke_rect``. An unsplit row reports the whole
    #: canvas at 0,0, which is what it always was.
    stroke_url: str | None
    stroke_rect: _RectOut | None
    text_url: str | None
    segments: list[_SegmentOut]
    avatars: list[_AvatarOut]
    cues: list[_CueOut]
    unsupported: list[str] = Field(default_factory=list)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


def _style_of(
    font_name: str | None,
    font_size: int | None,
    margin_v: int | None,
    primary_colour: str | None,
) -> SubtitleStyle:
    """The requested style, falling back field by field.

    Every field optional and every field defaulted from a fresh ``SubtitleStyle``: the UI sends
    only what the user moved, and sending all four on every drag would make the request carry
    values the client had to guess.
    """
    defaults = SubtitleStyle()
    return SubtitleStyle(
        font_name=font_name or defaults.font_name,
        font_size=defaults.font_size if font_size is None else font_size,
        margin_v=defaults.margin_v if margin_v is None else margin_v,
        primary_colour=primary_colour or defaults.primary_colour,
    )


@router.get("/versions/{version_id}/video/plan", response_model=VideoPlanOut)
def get_plan(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    font_name: str | None = Query(default=None),
    font_size: int | None = Query(default=None, ge=16, le=160),
    margin_v: int | None = Query(default=None, ge=0, le=600),
    primary_colour: str | None = Query(default=None),
    burn_subtitles: bool = Query(default=True),
) -> VideoPlanOut:
    """The whole film as one description, on the output timeline.

    The subtitle style is a query parameter for the same reason the still preview's is: the user
    drags that slider *while deciding*, and writing to 设置 first would make every drag a
    database write. It is part of the plan digest, so changing it produces new artefact URLs
    rather than a new picture under an old name.
    """
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    plan = video_plan.build(
        session,
        settings,
        version,
        style=_style_of(font_name, font_size, margin_v, primary_colour),
        burn_subtitles=burn_subtitles,
    )
    return VideoPlanOut.model_validate(video_plan.payload(plan))


@router.get("/video/plan/file/{name}")
def plan_file(
    name: str,
    request: Request,
    project_id: str = Query(),
) -> FileResponse:
    """Serve one artefact the plan baked.

    ``immutable`` caching is safe **because the name carries the digest**: a given name can never
    mean two different pictures, so there is nothing to revalidate. That is also why nothing in
    this directory is ever swept — a stale file is unreachable rather than wrong (§19.11 trap 24).
    """
    if not _SAFE_NAME.match(name):
        raise AppError(
            "plan_file_invalid",
            "文件名不合法",
            hint="预览计划只会请求它自己烘出来的文件。",
            status=400,
        )
    settings: Settings = request.app.state.settings
    directory = video_plan.bake_dir(settings, project_id)
    path = directory / name
    if not path.is_file():
        raise AppError(
            "plan_file_missing",
            "这张预览图还没生成",
            hint="重新请求一次预览计划；反复出现就检查导出目录是否可写。",
            status=404,
        )
    return FileResponse(
        path,
        media_type="image/png" if name.endswith(".png") else "image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/versions/{version_id}/video/cue/{digest}/{index}.png")
def cue_image(
    version_id: str,
    digest: str,
    index: int,
    request: Request,
    session: Session = Depends(_session_dependency),
    font_name: str | None = Query(default=None),
    font_size: int | None = Query(default=None, ge=16, le=160),
    margin_v: int | None = Query(default=None, ge=0, le=600),
    primary_colour: str | None = Query(default=None),
) -> FileResponse:
    """Bake (or serve) one subtitle cue as a transparent band PNG (§19.31.4).

    **On demand, because the alternative is 772 bakes** to serve a preview that shows three at a
    time. A band costs **77 ms** end to end — ASS file, ffmpeg, PNG — of which half is ffmpeg's
    startup, which is inside the budget the browser needs.

    The path carries the **plan digest**, and that is checked against a freshly built plan before
    anything is baked. Two reasons, both about not lying: an index from a stale plan must fail
    loudly rather than draw somebody else's subtitle, and a URL is only cacheable for ever if a
    given path can never mean two different pictures.
    """
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    style = _style_of(font_name, font_size, margin_v, primary_colour)
    plan = video_plan.build(session, settings, version, style=style)
    if plan.digest != digest:
        raise AppError(
            "plan_digest_stale",
            "预览计划和现在的成片对不上",
            hint="刷新一下预览；如果还不对，说明成片刚被改过。",
            status=409,
        )
    if index < 0 or index >= len(plan.cues):
        raise AppError(
            "cue_out_of_range",
            "这条字幕不存在",
            hint=f"当前有 {len(plan.cues)} 条字幕，索引从 0 开始。",
            status=404,
        )
    cue = plan.cues[index]
    target = video_plan.bake_dir(settings, version.project_id) / video_plan.cue_name(
        digest, index
    )
    if not target.is_file():
        cue_png.bake(
            settings.ffmpeg_path,
            target=target,
            text=cue.text,
            style=style,
            canvas_w=plan.canvas_w,
            canvas_h=plan.canvas_h,
        )
    return _immutable(target)


@router.get("/versions/{version_id}/video/backdrop/{digest}/{position}.png")
def backdrop_image(
    version_id: str,
    digest: str,
    position: int,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    """Bake (or serve) the blurred backdrop for one segment (§19.31.3).

    Only the two blur fits need this: `contain`/`cover`/`stretch` are native CSS, and even inside
    a blur only the *backdrop* is ffmpeg's — the foreground on top of it is native again. Baked
    lazily because a plan is fetched for the whole film, while the browser only ever shows one
    segment at a time.

    ``position`` is the index in the **plan's** segment list (after the plate gaps are dropped),
    which is the same number the UI shows; a position past the end is a 404, not a clamp, because
    clamping would serve the wrong picture under a right-looking URL.
    """
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    plan = video_plan.build(session, settings, version)
    if plan.digest != digest:
        raise AppError(
            "plan_digest_stale",
            "预览计划和现在的成片对不上",
            hint="刷新一下预览；如果还不对，说明成片刚被改过。",
            status=409,
        )
    if position < 0 or position >= len(plan.segments):
        raise AppError(
            "backdrop_out_of_range",
            "这一段不存在",
            hint=f"当前有 {len(plan.segments)} 段素材，索引从 0 开始。",
            status=404,
        )
    segment = plan.segments[position]
    if segment.backdrop_url is None:
        raise AppError(
            "backdrop_not_needed",
            "这一段不需要模糊底",
            hint="只有「模糊铺满」和「模糊裁切」才需要，其他填充方式是浏览器自己画的。",
            status=404,
        )
    target = video_plan.bake_dir(settings, version.project_id) / video_plan.backdrop_name(
        digest, position, segment.fit
    )
    if not target.is_file():
        backdrop_media.bake(
            settings.ffmpeg_path,
            target=target,
            source=segment.source,
            kind=segment.kind,
            width=plan.box_w,
            height=plan.box_h,
        )
    return _immutable(target)


def _immutable(path: Path) -> FileResponse:
    """A response that may be cached for ever, because its name carries the digest.

    ``immutable`` is not an optimisation here — it is what makes scrubbing cheap. Without it the
    browser revalidates each of the three hundred cue bands a minute of playback touches.
    """
    return FileResponse(
        path,
        # PNG only: the backdrops are lossless because JPEG's full-range round trip shifts the
        # blurred ring by more than the comparison threshold (measured 4.5 % of it).
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
