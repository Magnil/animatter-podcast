"""The DOM preview's data model: one static description of the whole film (§19.31.2).

**Why this exists.** A still preview costs 0.63–0.93 s because ffmpeg must start, decode its
inputs and composite a frame. But the film does not change 24 times a second — only *which*
segment is on screen, which avatars are lit, and which cue is up. So the browser composites: a
stack of `<img>`/`<video>` layers, and per tick it flips a `src` or an `opacity`. Measured
144 fps / 0 dropped frames with a 4K source in the stack (§19.31.1).

**What measuring the real templates changed.** Three findings decided the design, all recorded
in §19.31.3–§19.31.4:

1. **Only the blurred backdrop needs ffmpeg.** `contain`, `cover` and `stretch` are native CSS
   (`object-fit`, with `cover` anchored `left top` — ffmpeg crops at `0:0`, not at the centre).
   For `blur_fill`/`blur_crop` the *foreground* is native too; only the blurred, dimmed
   backdrop has to be baked, because `boxblur` is not a gaussian and `eq=brightness` is not
   `brightness()`. On the sample that is 26 of 64 drawn segments, and a backdrop is cached by
   plan digest for ever.
2. **`kind="plate"` segments draw no element at all.** They exist so the *opaque* box chain has
   something in a gap; in a layer stack the plate is already underneath. Measured:
   `background.png` and `background_bare.png` agree inside the box rect in **0** pixels, so
   "draw the plate crop" and "draw nothing" are the same picture. `kind="transparent"`
   placeholders exist only to keep two ffmpeg chains aligned (§19.26) and draw nothing either.
3. **A style-only bottom band is the right shape for a cue.** Its height follows from
   `margin_v + lines × line advance`, both known from the style and the pure `wrap_cjk`, so the
   plan publishes every cue's rect **before** one byte is baked; the PNG arrives later.

**Why the layer decisions are made here, not in the browser.** Which avatar slots get an
element, whether the base is `background.png` or `background_bare.png`, and which name plates
the text layer contains are all choices the *export* makes (`_base_background`,
`avatar_tracks`, `draw_slots`). If the preview re-derived them they would eventually disagree,
and the disagreement would be invisible on a project without bound avatars.

**Why URLs are resolved here and not by the router.** A plan points at six kinds of file, and
each is served by a different route with different parameters (an asset needs its row id, a
deck page needs the deck id, the page id and the **box's** pixel size). Resolving them against
the project's actual rows makes a wrong URL impossible rather than merely unlikely: an
unmapped path raises instead of falling back to a guess. A route that took a path instead would
be a file-read primitive (§8).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.video_avatar import Interval
from animatter.errors import AppError
from animatter.media import cue_png
from animatter.media.box_track import KIND_PLATE, KIND_TRANSPARENT
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.models import Deck, Slide, TranscriptVersion, VideoAsset, VideoItem, VideoTemplate
from animatter.services import clipping as clipping_service
from animatter.services import decks as deck_service
from animatter.services import projects as project_service
from animatter.services import (
    subtitles,
    transcript_facts,
    video_avatars,
    video_box,
    video_layout,
    video_render,
)
from animatter.services import video as video_service
from animatter.services import video_templates as template_service

#: Sub-directory of the project's `video/out/` that holds plan artefacts. Kept apart from
#: `previews/` so sweeping stale stills can never delete something a plan URL points at.
#:
#: Unlike the still preview's directory, **nothing here is ever swept by age**: every name
#: carries the plan digest, so a stale file is unreachable rather than wrong. That is the
#: cheaper of the two guarantees and the one §19.11 trap 24 actually asks for.
BAKE_DIR = "bake"

DEFAULT_CANVAS = (1920, 1080)

#: The route that serves plan-baked files eagerly baked by `build` (the text overlay). Cue bands
#: and blurred backdrops are produced on demand instead, by their own routes, because baking them
#: up front would be 772 + 26 ffmpeg runs for a preview that shows three at a time.
PLAN_FILE_PREFIX = "/api/video/plan/file"

#: On-demand bake routes. The **digest is in the path**, which is what makes them cacheable for
#: ever: a given URL can never mean two different pictures, so an HTTP cache never has to
#: revalidate — and a changed template or timeline produces a different path, not a stale body.
CUE_ROUTE = "/api/versions/{version_id}/video/cue/{digest}/{index}.png"
BACKDROP_ROUTE = "/api/versions/{version_id}/video/backdrop/{digest}/{position}.png"

#: Fits the browser draws natively (`previewLayers.ts`), and the two needing a baked backdrop.
NATIVE_FITS = ("stretch", "cover", "contain")
NEEDS_BACKDROP = ("blur_fill", "blur_crop")


@dataclass(frozen=True)
class PlanSegment:
    """One stretch of box content, on the **output** clock."""

    start_ms: int
    end_ms: int
    kind: str
    fit: str
    placement: str
    loop: bool
    #: What the browser fetches.
    url: str
    #: Where the *server* finds it — the backdrop baker needs the file itself.
    source: Path
    #: The blurred backdrop's URL, for the two blur fits only.
    backdrop_url: str | None = None
    #: True when a **video** segment's backdrop is a still: the film's background moves and the
    #: preview's will not. Reported rather than hidden — 24.2 s of the sample is this case, and
    #: a preview that silently freezes a moving background is the disagreement this module
    #: exists to prevent (§19.32's degradation table).
    backdrop_frozen: bool = False
    #: For a looped video, the period the browser's own `loop` repeats on **in milliseconds**.
    #:
    #: A `<video loop>` wraps at the container duration; ffmpeg's `-stream_loop` wraps at the
    #: stream duration. On the sample the two differ by 32 ms (292.281 against 292.249 s) and by
    #: 83 ms (190.763 against 190.680 s), which after two loops is whole frames — the residual the
    #: consistency check kept reporting on the two long clip segments before the offset was
    #: reduced by this number instead of by the browser's own guess. Zero for anything unlooped.
    period_ms: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class PlanCue:
    """One subtitle, as a bottom band of known geometry (§19.31.4)."""

    start_ms: int
    end_ms: int
    #: Position in ``VideoPlan.cues`` — what the bake URL carries.
    index: int
    #: The band's top edge and height on the canvas. The browser lays the element out before the
    #: PNG exists, which is why this is geometry and not just a URL.
    top: int
    height: int
    #: Where the band will be baked. ``None`` until the browser asks for it.
    url: str | None = None
    #: The cue's own text, for the baker. Never serialised: 772 strings is ~60 KB the browser
    #: would only ever send back.
    text: str = ""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class PlanAvatar:
    """One avatar slot that actually changes state during the film."""

    name: str
    x: int
    y: int
    width: int
    height: int
    lit_url: str
    dim_url: str
    lit: Path
    dim: Path
    #: Lit stretches in output time, ascending and disjoint.
    intervals: tuple[Interval, ...]


@dataclass(frozen=True)
class VideoPlan:
    """Everything a DOM stage needs for the whole film."""

    digest: str
    version_id: str
    project_id: str
    canvas_w: int
    canvas_h: int
    box_x: int
    box_y: int
    box_w: int
    box_h: int
    output_duration_ms: int
    #: The compiled poster to start from, **with the media-box plate removed**: the bare twin
    #: whenever an avatar track exists (that is what makes an unbound slot an empty frame rather
    #: than a stale photo), otherwise the plain one.
    background_url: str
    #: The media-box plate, its own PNG at its own rect. `None` for a template compiled before
    #: round 137, where the plate is baked into the poster instead.
    plate_url: str | None
    plate_rect: dict[str, int] | None
    #: The frame stroke, cropped to its own bounds. Usually present; a template with no stroke
    #: layer has none, and the DOM stage simply draws one layer fewer.
    stroke_url: str | None
    stroke_rect: dict[str, int] | None
    #: The editable text, already resolved for the slots that are visible.
    text_url: str | None
    segments: tuple[PlanSegment, ...]
    avatars: tuple[PlanAvatar, ...]
    cues: tuple[PlanCue, ...]
    #: The eagerly baked text overlay, if the template has text slots. It is the one artefact a
    #: plan cannot defer: it is a whole-canvas image on every frame, not something the browser
    #: could fetch per instant.
    text: Path | None
    #: Segments whose source the browser may not decode. Empty in practice for the formats
    #: Chrome handles; the field exists so the UI can say 「此素材无法实时预览」 instead of
    #: showing a black rectangle (§19.32).
    unsupported: tuple[str, ...] = ()

    def segment_at(self, at_ms: int) -> PlanSegment | None:
        """The segment covering ``at_ms``, by binary search."""
        return _find(self.segments, at_ms)

    def cue_at(self, at_ms: int) -> PlanCue | None:
        """The cue covering ``at_ms``; cues never overlap, so at most one matches."""
        return _find(self.cues, at_ms)

    def lit_avatars(self, at_ms: int) -> set[str]:
        """Names of the slots that are lit at ``at_ms``."""
        out: set[str] = set()
        for avatar in self.avatars:
            if _covers(avatar.intervals, at_ms):
                out.add(avatar.name)
        return out


Span = TypeVar("Span", PlanSegment, PlanCue)


def _find(items: Sequence[Span], at_ms: int) -> Span | None:
    """Binary search over any list of ``[start_ms, end_ms)`` spans.

    Generic rather than typed twice: segments and cues are searched on every playback tick and
    the two lists have the same shape, so a second copy would be a second place for an
    off-by-one in the half-open comparisons.
    """
    low, high = 0, len(items) - 1
    while low <= high:
        middle = (low + high) // 2
        found = items[middle]
        if at_ms < found.start_ms:
            high = middle - 1
        elif at_ms >= found.end_ms:
            low = middle + 1
        else:
            return found
    return None


def _covers(intervals: Sequence[Interval], at_ms: int) -> bool:
    low, high = 0, len(intervals) - 1
    while low <= high:
        middle = (low + high) // 2
        piece = intervals[middle]
        if at_ms < piece.start_ms:
            high = middle - 1
        elif at_ms >= piece.end_ms:
            low = middle + 1
        else:
            return True
    return False


def loop_period_ms(settings: Settings, path: Path) -> int:
    """The period a looped clip actually repeats on, in milliseconds.

    **The stream's duration, not the container's** — this is the number that makes the two halves
    of the preview agree, and getting it from the wrong one is a whole-frame error:

    * ffmpeg's ``-stream_loop -1`` wraps at the *stream* duration (4 767 frames / 25 fps =
      190.680 s on the sample; 7 014 frames at 24 fps = 292.249 s on the other);
    * an ``HTMLMediaElement`` reports and loops on the *container* duration (190.763 s and
      292.281 s — 83 ms and 32 ms longer).

    The film therefore shows frame ``floor((t mod stream) × rate)``, and the browser is being
    *told* to seek to an offset we computed. Publishing the container period would make the
    browser seek 83 ms later than the film's own wrap, and after three loops that is four frames
    — which is exactly what the consistency check reported on the two long clip segments.

    Probed with one cheap ``ffprobe`` (50–120 ms, no decode) and only for segments that actually
    loop. Returns 0 when the probe fails, so the caller publishes 0 and the browser uses the raw
    offset: one frame out is better than a plan that fails to build.
    """
    try:
        completed = subprocess.run(
            [
                settings.ffprobe_path, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        text = (completed.stdout or "").strip().splitlines()
        if text and text[0] not in ("N/A", ""):
            return round(float(text[0]) * 1000)
        # A stream with no duration in its header (fragmented mp4): fall back to the container,
        # which is close enough for a period and never wrong by more than one frame per loop.
        completed = subprocess.run(
            [
                settings.ffprobe_path, "-v", "error",
                "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return round(float((completed.stdout or "").strip()) * 1000)
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return 0


def bake_dir(settings: Settings, project_id: str) -> Path:
    path = project_service.project_root(settings, project_id) / "video" / "out" / BAKE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def backdrop_name(digest: str, position: int, fit: str) -> str:
    """A baked backdrop's file name.

    Carries the plan digest, the segment's position and the fit, so a changed template, style or
    timeline produces a different name and the browser cannot keep showing the previous blur. The
    fit is in there because `blur_fill` and `blur_crop` share a backdrop chain today but must not
    be assumed to: the day they diverge, the name has to diverge with them.
    """
    return f"backdrop-{digest}-{position:03d}-{fit}.png"


def cue_name(digest: str, index: int) -> str:
    return f"cue-{digest}-{index:05d}.png"


def build(
    session: Session,
    settings: Settings,
    version: TranscriptVersion,
    *,
    style: SubtitleStyle | None = None,
    burn_subtitles: bool = True,
    facts: transcript_facts.TranscriptFacts | None = None,
) -> VideoPlan:
    """Describe the whole film for the DOM preview.

    ``burn_subtitles`` exists because the film can be exported without them, and a preview
    showing subtitles the export omits is the same class of lie as a wrong layer order.

    Costs **~210 ms**, almost all of it the slide renders and the text PNG, both of which are
    cached on disk by their own digest — so a plan for an unchanged project is a few file
    existence checks.
    """
    resolved = style or SubtitleStyle()
    template = template_service.require(session, version.project_id)
    if template.status != "ready":
        raise AppError("template_not_ready", "模板还没编译好", status=409)

    edl = clipping_service.build_edl(session, settings, version)
    items = video_service.list_items(session, version.project_id)
    # The **current** box and artwork rects: the layout editor moves both, and a plan built
    # from the compiled geometry puts the panel's own handles somewhere the film never goes.
    box_rect = video_layout.box_of(template)
    canvas_w = template.canvas_w or DEFAULT_CANVAS[0]
    canvas_h = template.canvas_h or DEFAULT_CANVAS[1]
    if facts is None:
        facts = transcript_facts.load(session, version.id)

    # Gap stretches are cut out of the plate, so the planner is told where the box sits inside
    # whichever image it gets (round 137: a plate-sized PNG, so a 1:1 crop).
    gap_image, gap_origin = template_service.box_source(template, box_rect)
    box_plan = video_box.plan(
        session,
        template_background=gap_image,
        template_origin=gap_origin,
        box_rect=box_rect,
        items=items,
        edl=edl,
        settings=settings,
        project_id=version.project_id,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )
    tracks = video_render.avatar_tracks(session, template, version, edl, facts=facts)
    plates = video_avatars.name_plates(session, template)
    directory = bake_dir(settings, version.project_id)
    text_png = video_render.text_png_for(
        template,
        work_dir=directory,
        width=canvas_w,
        height=canvas_h,
        visible_avatars={track.name for track in tracks} or None,
        avatar_names=plates,
    )

    digest = _digest(template, resolved, items, edl.output_duration_ms, tracks, plates)
    urls = _url_map(
        session,
        settings,
        template=template,
        project_id=version.project_id,
        box_w=box_plan.width,
        box_h=box_plan.height,
    )
    def plan_file(path: Path) -> str:
        return f"{PLAN_FILE_PREFIX}/{path.name}?project_id={version.project_id}"

    def url_of(path: Path) -> str:
        found = urls.get(str(path))
        if found is None:
            raise AppError(
                "plan_file_unroutable",
                f"不知道该用哪个接口提供 {path.name}",
                hint="这是内部契约断裂，请把这条日志一起报上来。",
                status=500,
            )
        return found

    periods: dict[Path, int] = {}

    def period_of(path: Path) -> int:
        # One probe per distinct clip, not per segment: a gallery's members share a source and a
        # film can loop the same clip in three blocks.
        if path not in periods:
            periods[path] = loop_period_ms(settings, path)
        return periods[path]

    def backdrop_of(position: int) -> str:
        return BACKDROP_ROUTE.format(
            version_id=version.id, digest=digest, position=position
        )

    segments = _flatten(box_plan, url_of=url_of, backdrop_of=backdrop_of, period_of=period_of)
    avatars = tuple(
        PlanAvatar(
            name=track.name,
            x=track.x,
            y=track.y,
            width=track.width,
            height=track.height,
            lit_url=url_of(track.lit_path),
            dim_url=url_of(track.dim_path),
            lit=track.lit_path,
            dim=track.dim_path,
            intervals=tuple(track.lit),
        )
        for track in tracks
    )

    cues: tuple[PlanCue, ...] = ()
    if burn_subtitles:
        built, _issues = subtitles.build_cues(
            session,
            version.id,
            time_map=edl.time_map() if edl.removed_ms else None,
            facts=facts,
        )
        cues = tuple(
            PlanCue(
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                index=index,
                top=canvas_h - _band(cue.text, resolved, canvas_w, canvas_h),
                height=_band(cue.text, resolved, canvas_w, canvas_h),
                url=CUE_ROUTE.format(
                    version_id=version.id, digest=digest, index=index
                ),
                text=cue.text,
            )
            for index, cue in enumerate(built)
        )

    background = video_render._base_background(template, tracks)
    plate, stroke = video_layout.art_of(template)

    def art_url(item: template_service.Artwork | None) -> str | None:
        # `None` rather than a raise: a template that legitimately has no stroke (or one
        # compiled before the split) is not a broken contract, it is a different shape.
        if item is None or not item.path.is_file():
            return None
        return url_of(item.path)

    return VideoPlan(
        digest=digest,
        version_id=version.id,
        project_id=version.project_id,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        box_x=int(box_rect.get("x") or 0),
        box_y=int(box_rect.get("y") or 0),
        box_w=int(box_rect.get("w") or 0),
        box_h=int(box_rect.get("h") or 0),
        output_duration_ms=edl.output_duration_ms,
        background_url=url_of(background),
        plate_url=art_url(plate),
        plate_rect=None if plate is None or not plate.path.is_file() else plate.as_rect(),
        stroke_url=art_url(stroke),
        stroke_rect=None if stroke is None or not stroke.path.is_file() else stroke.as_rect(),
        text_url=None if text_png is None else plan_file(text_png),
        segments=segments,
        avatars=avatars,
        cues=cues,
        text=text_png,
    )


def _band(text: str, style: SubtitleStyle, canvas_w: int, canvas_h: int) -> int:
    return cue_png.band_height(text, style, play_w=canvas_w, canvas_h=canvas_h)


def _url_map(
    session: Session,
    settings: Settings,
    *,
    template: VideoTemplate,
    project_id: str,
    box_w: int,
    box_h: int,
) -> dict[str, str]:
    """``str(path) → url`` for every kind of file a plan can point at.

    Built from the project's **rows**, not from a naming convention: a deck page's URL needs
    the deck id and the page id, and its PNG's name contains the size and the typeface pair
    (`{slide_id}-{w}x{h}-{tag}.png`). Deriving that from the file name would break the moment a
    deck is re-set in another face, and it would silently point at the editor's thumbnail size
    instead of the box's.
    """
    urls: dict[str, str] = {}

    for row in session.scalars(
        select(VideoAsset).where(VideoAsset.project_id == project_id)
    ).all():
        urls[str(Path(row.path))] = f"/api/projects/{project_id}/video/assets/{row.id}/file"

    for deck in session.scalars(select(Deck).where(Deck.project_id == project_id)).all():
        folder = deck_service.slides_dir(settings, project_id, deck.id)
        for page in session.scalars(select(Slide).where(Slide.deck_id == deck.id)).all():
            for candidate in folder.glob(f"{page.id}-*"):
                urls[str(candidate)] = (
                    f"/api/projects/{project_id}/decks/{deck.id}/slides/{page.id}/image"
                    # The size the plan asked for: the slide route caches by size, and a
                    # mismatch serves a page drawn for the editor's thumbnail (§19.16).
                    f"?width={box_w}&height={box_h}"
                )

    # Compiled template artefacts: the poster, its bare twin, the artwork overlay, the avatar
    # photos — and the `edits/` copies of replaced photos, which `asset_path` also serves.
    base = Path(template.background_path).parent
    for folder in (base, base / "edits"):
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            if candidate.is_file():
                urls.setdefault(
                    str(candidate),
                    f"/api/projects/{project_id}/video/template/asset/{candidate.name}",
                )
    return urls


def _digest(
    template: VideoTemplate,
    style: SubtitleStyle,
    items: Sequence[VideoItem],
    output_duration_ms: int,
    tracks: list[video_render.AvatarTrack],
    avatar_names: dict[str, str],
) -> str:
    """A hash of everything that changes what the plan describes.

    Broader than the still digest on purpose: a still depends only on the frame it draws, while
    the plan describes the **whole film**, so moving a block that is not on screen must change
    it. The rest follows §19.11 trap 24 — an artefact named after only *some* of its inputs
    outlives the change that should have replaced it.
    """
    from animatter.services import video_edits

    parts = [
        template.source_sha256 or "",
        template.box_rect or "",
        # Where the plate and the stroke actually are. `box_rect` alone is not enough: moving
        # the artwork without moving the box still changes the picture, and the layout editor
        # (§19.38) can move either.
        # Current artwork geometry, not the compiled one: the layout editor moves the plate and
        # the stroke with the box, so the signature has to come from the same module that
        # applies the overrides or the preview keeps serving the pre-drag digest.
        video_layout.art_signature(template),
        template.bindings_json or "",
        # Which includes the arrangement itself: a moved title changes pixels without touching
        # a single character of text, and a digest that only hashes the text would hand back the
        # old frame under the old URL (§19.11 trap 24).
        video_edits.signature(template),
        video_layout.layout_of(template).signature(),
        json.dumps(avatar_names, sort_keys=True, ensure_ascii=False),
        ",".join(sorted(track.name for track in tracks)) or "-",
        f"{style.font_name}/{style.font_size}/{style.primary_colour}/{style.outline}/"
        f"{style.shadow}/{style.margin_v}/{style.margin_l}/{style.margin_r}/{int(style.bold)}",
        str(output_duration_ms),
        hashlib.sha256(
            ";".join(
                f"{item.start_ms}-{item.end_ms}:{item.kind}:{item.fit}:{item.placement}:"
                f"{item.asset_id or ''}:{item.deck_id or ''}:{item.gallery_json}"
                for item in items
            ).encode()
        ).hexdigest(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _flatten(
    plan: video_box.BoxPlan,
    *,
    url_of: Callable[[Path], str],
    backdrop_of: Callable[[int], str],
    period_of: Callable[[Path], int] | None = None,
) -> tuple[PlanSegment, ...]:
    """Flatten the box plan onto the output clock, dropping the stretches that draw nothing.

    The **positions** in this tuple are the browser's segment indices, and they are also what a
    backdrop's file name carries. Numbering them after the drop (rather than by the box plan's
    own index) means the count matches what the UI sees, which is what makes 「第 12 段」 in a
    screenshot and in the JSON the same thing.
    """
    out: list[PlanSegment] = []
    cursor = 0
    for segment in plan.segments:
        start = cursor
        cursor += segment.duration_ms
        if segment.kind in (KIND_PLATE, KIND_TRANSPARENT):
            # Nothing to draw: the plate is already the base layer, and a placeholder is a
            # transparent filter that exists only to keep two ffmpeg chains aligned (§19.26).
            continue
        source = plan.sources[segment.input_index]
        backdrop_url: str | None = None
        if segment.fit in NEEDS_BACKDROP:
            backdrop_url = backdrop_of(len(out))
        out.append(
            PlanSegment(
                start_ms=start,
                end_ms=cursor,
                kind=segment.kind,
                fit=segment.fit,
                placement=segment.placement,
                loop=segment.loop,
                url=url_of(source),
                source=source,
                backdrop_url=backdrop_url,
                backdrop_frozen=segment.kind == "video" and backdrop_url is not None,
                period_ms=(
                    period_of(source)
                    if period_of is not None and segment.loop and segment.kind == "video"
                    else 0
                ),
            )
        )
    return tuple(out)


def payload(plan: VideoPlan) -> dict[str, object]:
    """The JSON the browser reads.

    A separate function rather than ``asdict``: the shape is a contract with the frontend, and
    keeping it in one place means the internal model can gain a field (the baker's paths, the
    cue texts) without silently shipping it.
    """
    return {
        "digest": plan.digest,
        "version_id": plan.version_id,
        "canvas": {"w": plan.canvas_w, "h": plan.canvas_h},
        "box": {"x": plan.box_x, "y": plan.box_y, "w": plan.box_w, "h": plan.box_h},
        "output_duration_ms": plan.output_duration_ms,
        "background_url": plan.background_url,
        "plate_url": plan.plate_url,
        "plate_rect": plan.plate_rect,
        "stroke_url": plan.stroke_url,
        "stroke_rect": plan.stroke_rect,
        "text_url": plan.text_url,
        "segments": [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "kind": segment.kind,
                "fit": segment.fit,
                "placement": segment.placement,
                "loop": segment.loop,
                "url": segment.url,
                "backdrop_url": segment.backdrop_url,
                "backdrop_frozen": segment.backdrop_frozen,
                # The period a browser's own `loop` repeats on — it cannot derive this correctly
                # (see `PlanSegment.period_ms`), and getting it wrong is a whole-frame error.
                "period_ms": segment.period_ms,
            }
            for segment in plan.segments
        ],
        "avatars": [
            {
                "name": avatar.name,
                "rect": {
                    "x": avatar.x,
                    "y": avatar.y,
                    "w": avatar.width,
                    "h": avatar.height,
                },
                "lit_url": avatar.lit_url,
                "dim_url": avatar.dim_url,
                "intervals": [
                    {"start_ms": piece.start_ms, "end_ms": piece.end_ms}
                    for piece in avatar.intervals
                ],
            }
            for avatar in plan.avatars
        ],
        "cues": [
            {
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "index": cue.index,
                "top": cue.top,
                "height": cue.height,
                "url": cue.url,
            }
            for cue in plan.cues
        ],
        "unsupported": list(plan.unsupported),
    }
