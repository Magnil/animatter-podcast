"""Video export and still preview (§19.5, §19.7).

The export is a background job for the same reason the audio render is: a one-hour
film takes ~14 minutes of encoding, and holding a SQLite write transaction for that
long makes every other write in the app fail with ``database is locked``. The
pattern is copied from ``services/renders.py`` rather than shared with it, because
the two have different inputs (an EDL vs an EDL *plus* a template and a timeline) and
different progress denominators; a common base class would only add indirection.

Order of work, and why:

1. **Audio first.** If there is no rendered audio, there is nothing to mux, and
   discovering that after fourteen minutes of x264 is unacceptable. The audio render
   is reused when an identical EDL was already exported — it is keyed by EDL hash.
2. **Cues → ASS.** Cues are built in *output* time (mapped through the TimeMap), so
   subtitles cannot drift away from the cut speech.
3. **Silent video.** One ffmpeg, one filtergraph in a script file (§19.11 陷阱 1).
4. **Mux.** ``-c copy``; measured 0.24 s per 60 s.

Still preview is a separate, request-time path: ~200 ms per frame, so the UI can call
it as the playhead moves without a job row.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import Edl
from animatter.core.video_avatar import Interval, lit_at, track_pieces
from animatter.core.video_avatar import pieces_in as track_pieces_in
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import avatar_track, box_track, video_chunks
from animatter.media import text_overlay as overlay_media
from animatter.media import video as video_media
from animatter.media.subtitle_ass import AssCue, SubtitleStyle, write_ass
from animatter.models import Media, Render, TranscriptVersion, VideoItem, VideoTemplate
from animatter.services import clipping as clipping_service
from animatter.services import projects as project_service
from animatter.services import subtitles as subtitle_service
from animatter.services import video as video_service
from animatter.services import video_avatars, video_box, video_edits
from animatter.services import video_templates as template_service

log = get_logger(__name__)

#: Two slots, same reasoning as the audio renderer: one encode already saturates
#: ffmpeg, and two covers "export while checking another episode".
_MAX_WORKERS = 2

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="video")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_futures: dict[str, Future[None]] = {}

_ACTIVE = ("queued", "running")


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


@dataclass(frozen=True)
class ExportOptions:
    """Everything the 「导出视频」 dialog can change."""

    #: ``None`` means "use the defaults"; a mutable-ish instance as a default would
    #: be shared across calls, and RUF009 is right to reject one.
    subtitle_style: SubtitleStyle | None = None
    burn_subtitles: bool = True
    lossless_audio: bool = False

    def style(self) -> SubtitleStyle:
        return self.subtitle_style or SubtitleStyle()


@dataclass(frozen=True)
class AvatarTrack:
    """One avatar slot that changes state during the film (P3).

    ``lit`` holds the intervals in **output** time: the EDL has already been applied, so a
    speaker whose only sentences were cut produces no interval at all and the slot is left
    exactly as the compiled background drew it.
    """

    name: str
    x: int
    y: int
    width: int
    height: int
    lit_path: Path
    dim_path: Path
    lit: list[Interval]


@dataclass(frozen=True)
class PreparedVideo:
    """The inputs an encode needs, resolved once so the worker never re-reads them."""

    template: VideoTemplate
    box_rect: dict[str, int]
    items: list[VideoItem]
    edl: Edl
    duration_ms: int
    #: Empty unless at least one avatar slot is bound to a speaker who talks.
    avatars: list[AvatarTrack] = field(default_factory=list)
    #: Speaker name per bound avatar group — what each name plate says (round 99).
    avatar_names: dict[str, str] = field(default_factory=dict)
    #: Deliberately **absent**: a slot with no binding leaves its frame empty. Replacing the
    #: face with a stale PSD photo would contradict the binding the user just cleared — the
    #: sample's 4 slots with 2 speakers must show 2 faces and 2 empty frames, not 4 faces.
    #: ``_base_background`` already switches to ``background_bare.png`` whenever a track exists,
    #: which is what makes the empty frames possible at all.

    @property
    def background_path(self) -> Path:
        """Which compiled poster to start from — see :func:`_base_background`."""
        return _base_background(self.template, self.avatars)


def output_dir(settings: Settings, project_id: str) -> Path:
    path = project_service.project_root(settings, project_id) / "video" / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- preparing ----------------------------------------------------------


def _audio_for(
    session: Session, settings: Settings, version: TranscriptVersion, *, lossless: bool
) -> Path:
    """The audio for this exact EDL: an earlier export when one exists, otherwise one made now.

    Reusing an export is what makes ``-c copy`` cheap *and* what guarantees the film sounds
    exactly like the audio the user already approved. But requiring that export first was a
    rule about the app's plumbing, not about the user's work (round 110): someone who only
    proofread subtitles and then asked for the film got 409 with 「先导出成片音频」. An empty
    edit means "keep everything", so the render is one ffmpeg call over the whole file — there
    was never a reason to make the user do it by hand first.
    """
    from animatter.core.edl import edl_signature

    edl = clipping_service.build_edl(session, settings, version)
    signature = edl_signature(edl)
    candidates = session.scalars(
        select(Render)
        .where(Render.version_id == version.id, Render.status == "done")
        .order_by(Render.created_at.desc())
    ).all()
    for row in candidates:
        if row.edl_hash != signature:
            continue
        path = Path(row.out_path)
        if not path.is_file():
            continue
        if lossless and path.suffix != ".wav":
            continue
        if not lossless and path.suffix == ".wav":
            # A WAV can still be muxed, but it is 10× the size; prefer the m4a.
            continue
        return path

    # Nothing exported for this EDL yet, so render one. This is the *only* place the film
    # pipeline encodes audio on its own; a later export of the same EDL finds this file and
    # reuses it, so the cost is paid once either way.
    log.info("video.audio_render", version_id=version.id, lossless=lossless)
    return _render_audio(session, settings, version, edl=edl, lossless=lossless)


def _render_audio(
    session: Session,
    settings: Settings,
    version: TranscriptVersion,
    *,
    edl: Edl,
    lossless: bool,
) -> Path:
    """Render the EDL to a file and record a ``Render`` row for it.

    The row is recorded rather than just the file: the audio exporter's cache looks up
    ``render.out_path`` by EDL signature, so writing one here is what lets a later
    「导出成片音频」 answer instantly instead of encoding the same thing again.
    """
    from animatter.core.edl import edl_signature
    from animatter.media import render as render_media

    media = session.get(Media, version.media_id)
    if media is None:
        raise AppError("media_not_found", "找不到对应的音频", status=404)
    source = Path(media.path)
    if not source.is_file():
        raise AppError(
            "media_file_missing",
            "源音频文件不在了",
            hint="重新导入这期的音频。",
            status=404,
        )

    out_dir = project_service.project_root(settings, version.project_id) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if lossless else ".m4a"
    target = out_dir / f"{version.id}-{edl_signature(edl)[:12]}{suffix}"

    # Committed before ffmpeg starts: the file is the deliverable, and a crash mid-encode
    # must not leave a row claiming a finished render.
    record = Render(
        id=new_id("rnd"),
        project_id=version.project_id,
        version_id=version.id,
        edl_hash=edl_signature(edl),
        out_path=str(target),
        duration_ms=edl.output_duration_ms,
        status="running",
        progress=0.0,
        created_at=utcnow_iso(),
    )
    session.add(record)
    session.commit()

    try:
        result = render_media.render(
            settings.ffmpeg_path,
            source=source,
            edl=edl,
            target=target,
            sample_rate=media.sample_rate or 44_100,
            channels=media.channels or 2,
            lossless=lossless,
        )
    except AppError as exc:
        record.status = "failed"
        record.error = f"{exc.message}：{exc.hint}"
        session.commit()
        raise

    record.status = "done"
    record.progress = 1.0
    record.duration_ms = result.duration_ms
    session.commit()
    return target


def prepare(
    session: Session,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    lossless_audio: bool = False,
) -> PreparedVideo:
    """Resolve template, timeline, EDL and audio — or raise a usable error."""
    version = clipping_service.get_version(session, version_id)
    template = template_service.require(session, project_id)
    if template.status != "ready":
        raise AppError(
            "template_not_ready",
            "模板还没编译好",
            hint="等编译完成，或者重新导入这份 PSD。",
            status=409,
        )

    edl = clipping_service.build_edl(session, settings, version)
    items = video_service.list_items(session, project_id)

    tracks = avatar_tracks(session, template, version, edl)
    return PreparedVideo(
        template=template,
        box_rect=template_service.box_rect_of(template),
        items=items,
        edl=edl,
        duration_ms=edl.output_duration_ms,
        avatars=tracks,
        avatar_names=video_avatars.name_plates(session, template),
    )


def _cues_for(
    session: Session, version_id: str, edl: Edl
) -> list[AssCue]:
    """The burned-in subtitles in **output** time, before any chunking.

    Kept separate from ``_ass_for`` because a chunked export needs the cues themselves: each
    chunk gets its own ASS re-based to chunk-local time (§19.27). Building one whole-film ASS
    and slicing the text would put every cue in every chunk — the burn is relative to the
    frame's PTS, and every chunk starts at 0.
    """
    time_map = edl.time_map() if edl.removed_ms else None
    cues, _issues = subtitle_service.build_cues(session, version_id, time_map=time_map)
    return [AssCue(start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text) for cue in cues]


def _ass_for(
    session: Session,
    version_id: str,
    edl: Edl,
    style: SubtitleStyle,
    target: Path,
) -> Path:
    """Build the burned-in subtitle file in output time."""
    ass_cues = _cues_for(session, version_id, edl)
    if not ass_cues:
        raise AppError(
            "no_subtitle_cues",
            "这个版本没有可烧入的字幕",
            hint="先完成转写并确认字幕内容。",
        )
    return write_ass(target, ass_cues, style=style)


def _layers(template: VideoTemplate) -> list[video_media.Layer]:
    """Static overlays above the box content: in P1 just the frame stroke.

    Ordered **after** the box in the graph, which is what makes the stroke sit on top of
    the picture instead of being covered by it.

    The editable text is **not** here: it has to be drawn after the avatars (an avatar's rect
    contains its own name plate, so text drawn first is covered), and this function cannot
    express an ordering that interleaves with a list it does not receive. It is passed to
    ``render``/``render_still`` as their own ``text=`` argument instead.
    """
    path = Path(template.overlay_path) if template.overlay_path else None
    if path is None or not path.is_file():
        return []
    return [video_media.Layer(path=path, x=0, y=0)]


def text_png_for(
    template: VideoTemplate,
    *,
    work_dir: Path,
    width: int,
    height: int,
    visible_avatars: set[str] | None = None,
    avatar_names: dict[str, str] | None = None,
) -> Path | None:
    """Draw the editable text for this template, or ``None`` when it has none.

    ``None`` rather than an empty overlay: a transparent PNG still costs one input, one
    overlay filter and a scale per frame, and a template with no text slots is a legitimate
    shape (the sample has nine, but nothing guarantees that).

    The file name carries the edit signature, so a changed title produces a different file
    and therefore a different cache key. Same lesson as §19.11 trap 24: an artefact named
    only by position outlives the change that should have replaced it.

    ``avatar_names`` and ``visible_avatars`` are part of the name for the same reason. The
    name plate now follows the **speaker** (round 99), so renaming 呈AA changes the pixels
    while the edits blob does not move at all — without this the panel would show the new
    name and the poster the old one.
    """
    from animatter.services import video_edits

    slots = video_edits.draw_slots(
        template, visible_avatars=visible_avatars, avatar_names=avatar_names
    )
    if not slots:
        return None
    hidden = "-" if visible_avatars is None else f"-{len(visible_avatars)}"
    plates = hashlib.sha256(
        json.dumps(avatar_names or {}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    target = work_dir / f"text-{video_edits.signature(template)}{hidden}-{plates}.png"
    if target.is_file():
        return target
    return overlay_media.render(target=target, width=width, height=height, slots=slots)


def _box(
    session: Session,
    *,
    settings: Settings,
    template: VideoTemplate,
    box_rect: dict[str, int],
    items: list[VideoItem],
    edl: Edl,
) -> video_box.BoxPlan:
    """Plan the media-box chain for this export (§19.5).

    Returns an *empty* plan rather than raising when the project has no blocks: a film
    with a background and subtitles but an empty box is a legitimate export, and refusing
    it would make the feature regress for anyone who has not started using the box yet.

    ``settings``/``project_id`` are what let a ``slide`` block draw its pages: the PNGs are
    rendered at the box's exact size on the way in, so the overlay never rescales them.
    """
    return video_box.plan(
        session,
        template_background=Path(template.background_path),
        box_rect=box_rect,
        items=items,
        edl=edl,
        settings=settings,
        project_id=template.project_id,
        # The canvas, not the box: a 全屏 block is normalised into the frame (§19.25).
        canvas_w=template.canvas_w or 1920,
        canvas_h=template.canvas_h or 1080,
    )



def _box_chain(plan: video_box.BoxPlan) -> box_track.BoxChain:
    """The lower chain: what is drawn **under** the avatars and the text (§19.26).

    With no 全屏 block this is the whole timeline, built exactly as it always was — the opaque,
    box-sized chain. That is the branch every existing project takes, and it is why the feature
    changed no pixels for anyone who does not use it.
    """
    if plan.split is None:
        return box_track.build_chain(
            plan.segments,
            width=plan.width,
            height=plan.height,
            x=plan.box_x,
            y=plan.box_y,
        )
    return _canvas_chain(plan, plan.split.lower)


def _top_chain(plan: video_box.BoxPlan) -> box_track.BoxChain | None:
    """The upper chain: the 全屏 blocks, drawn **over** the avatars and the text.

    ``None`` when nothing is 全屏, so the caller passes nothing and the graph keeps the shape it
    had before the feature existed.
    """
    if plan.split is None:
        return None
    return _canvas_chain(plan, plan.split.upper, out_label="topout")


def _canvas_chain(
    plan: video_box.BoxPlan,
    segments: tuple[box_track.BoxSegment, ...],
    *,
    out_label: str = "boxout",
) -> box_track.BoxChain:
    """One half of a split plan, at canvas size.

    The half's input list is its own (`split.lower_sources` / `split.upper_sources`), and the
    segment indices already point into it — see `video_box._split_placement`, which is where
    the re-basing happens. That is deliberate: doing it here would mean this function had to
    know which half it was handed.
    """
    return box_track.build_canvas_chain(
        list(segments),
        canvas_w=plan.canvas_w,
        canvas_h=plan.canvas_h,
        box_w=plan.width,
        box_h=plan.height,
        box_x=plan.box_x,
        box_y=plan.box_y,
        # Two chains in one graph must not share a label: uild_filtergraph reads the lower
        # one as [boxout] and the upper as [topout], and a duplicate makes ffmpeg report
        # "output 0 (boxout) unconnected" — an error that names neither chain.
        out_label=out_label,
    )

def avatar_tracks(
    session: Session,
    template: VideoTemplate,
    version: TranscriptVersion,
    edl: Edl,
) -> list[AvatarTrack]:
    """Which avatar slots change state, and when (§19.5, P3).

    Slots without a binding, and slots bound to a speaker who never talks in the film, are
    **absent**: the compiled background already shows their photo, and building a constant
    track for them would cost a full-length clip for no visible change. On the sample
    template (4 slots / 2 speakers) that is half of them.
    """
    lit_by_slot = video_avatars.resolve(session, template, version, edl)
    if not lit_by_slot:
        return []

    slots = {slot.name: slot for slot in video_avatars.bound_slots(session, template)}
    out: list[AvatarTrack] = []
    for name, lit in lit_by_slot.items():
        slot = slots.get(name)
        if slot is None:
            continue
        out.append(
            AvatarTrack(
                name=name,
                x=slot.x,
                y=slot.y,
                # The track is rendered at the PNGs' own size (236×249 on the sample), and
                # the slot rect is where it goes (177×188): the overlay scales it, exactly
                # as the compile step scaled the static photo.
                width=slot.width,
                height=slot.height,
                lit_path=slot.lit_path,
                dim_path=slot.dim_path,
                lit=lit,
            )
        )
    # Stable order so the same bindings always produce the same input numbering (and
    # therefore the same command line, which is what the determinism test relies on).
    out.sort(key=lambda item: (item.y, item.x, item.name))
    return out


def _avatar_layers(
    settings: Settings,
    tracks: list[AvatarTrack],
    *,
    duration_ms: int,
    work_dir: Path,
    start_ms: int = 0,
    end_ms: int | None = None,
    tag: str = "",
) -> list[video_media.AvatarLayer]:
    """Render one lit/dim track per bound slot, ready to overlay.

    Measured 1.5 s and 55 MB per 600 s of track, so the cost is small next to the encode
    itself. Rendering them here rather than in ``prepare`` keeps the request that queues an
    export fast: the user is watching a progress bar either way.

    ``start_ms``/``end_ms`` render a **short track for one chunk** instead of the whole film
    (§19.27). Seeking the whole-film track into a chunk does not work: it is a sparse VFR clip
    (298 frames over 61 minutes), and `-ss <start> -t <seconds>` makes ffmpeg both drop frames
    and end the stream early. Measured on 费那奇MV: a 133 s window declared with `-t` yielded 7
    of 298 frames, last at 67 s, and because the overlay uses `eof_action=pass` the avatar then
    stopped being drawn for the rest of the chunk — the "avatar frame vanishes" bug. A fresh
    handful of stills per chunk has no seek to get wrong, and `concat` is exact.

    ``tag`` disambiguates the file names; the same slot appears in every chunk.
    """
    layers: list[video_media.AvatarLayer] = []
    for track in tracks:
        if end_ms is None:
            sequence = track_pieces(track.lit, total_ms=duration_ms)
        else:
            sequence = track_pieces_in(track.lit, start_ms=start_ms, end_ms=end_ms)
        if not sequence:
            # A chunk with no lit piece and no dim piece is impossible (the sequence tiles the
            # slice), but an empty list would make `build_track` raise; skipping is the honest
            # answer for a zero-length chunk.
            continue
        pieces = avatar_track.pieces_of(
            lit=[track.lit_path], dim=track.dim_path, sequence=sequence
        )
        target = work_dir / f"avatar-{_slug(track.name)}{tag}.mov"
        avatar_track.build_track(
            settings.ffmpeg_path, target=target, pieces=pieces, work_dir=work_dir
        )
        layers.append(
            video_media.AvatarLayer(
                path=target, x=track.x, y=track.y, width=track.width, height=track.height
            )
        )
    return layers


def _avatar_stills(
    tracks: list[AvatarTrack], *, at_ms: int
) -> list[video_media.AvatarLayer]:
    """One lit-or-dim PNG per slot at ``at_ms`` — the still preview's cheap path."""
    return [
        video_media.AvatarLayer(
            path=track.lit_path if lit_at(track.lit, at_ms) else track.dim_path,
            x=track.x,
            y=track.y,
            width=track.width,
            height=track.height,
            is_video=False,
        )
        for track in tracks
    ]


def _slug(name: str) -> str:
    """A file-name-safe slot name.

    Slots are named from PSD layer names, so they hold CJK, spaces and ``拷贝``. Written
    as-is, the concat script and the ``.mov`` would both be fine, but a name with a path
    separator would not — and ``Path(name).name`` is cheaper than debugging that.
    """
    cleaned = "".join(char if char.isalnum() else "-" for char in name).strip("-")
    return cleaned[:40] or "avatar"


def _base_background(template: VideoTemplate, tracks: list[AvatarTrack]) -> Path:
    """Which compiled poster to start from.

    With any bound avatar this **must** be ``background_bare``: ``background.png`` has the
    PSD's own photos flattened in, so a slot that goes dim would show the bright photo
    underneath the dim overlay — a panel that dims to *almost* dim.

    The recomposition was checked pixel-for-pixel: bare plus all four lit PNGs at their
    rects differs from ``background.png`` in **0** pixels, so switching bases changes
    nothing until a slot actually changes state. If ``background_bare.png`` is missing
    (a template compiled before P1 finished writing it), fall back rather than fail: the
    film is wrong by one dim photo, not broken.
    """
    if not tracks:
        return Path(template.background_path)
    bare = Path(template.background_path).with_name("background_bare.png")
    if bare.is_file():
        return bare
    log.warning("video.bare_background_missing", template=template.id)
    return Path(template.background_path)


# --- still preview ------------------------------------------------------


def preview_still(
    session: Session,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    at_ms: int,
    style: SubtitleStyle | None = None,
    burn_subtitles: bool = True,
) -> Path:
    """Composite one frame at ``at_ms`` on the output timeline.

    Request-time, not a job: a still costs ~200 ms, which is the entire reason v1
    promises a still rather than real-time playback. The user drags the playhead, lets
    go, and sees the real layout — from the same filtergraph the export uses, so what
    they approve is what they get.

    **The file name carries a digest of the inputs.** It used to be ``still-{at_ms}.png``,
    which meant tuning 字幕字号 rewrote the bytes under an unchanged URL: the browser saw
    the same string, never re-fetched, and the user watched a stale frame. Naming the file
    after what produced it makes the URL change exactly when the picture would, and keeps
    the reverse promise too — the same style at the same playhead is the same file.
    """
    version = clipping_service.get_version(session, version_id)
    template = template_service.require(session, project_id)
    if template.status != "ready":
        raise AppError("template_not_ready", "模板还没编译好", status=409)

    resolved_style = style or SubtitleStyle()
    edl = clipping_service.build_edl(session, settings, version)
    out_dir = output_dir(settings, project_id) / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)

    if burn_subtitles:
        # One ASS file at a fixed path, rewritten per request: it is regenerated by every
        # still anyway, and its content is the style the digest already tracks.
        _ass_for(session, version_id, edl, resolved_style, out_dir / "preview.ass")

    plan = _box(
        session,
        settings=settings,
        template=template,
        box_rect=template_service.box_rect_of(template),
        items=video_service.list_items(session, project_id),
        edl=edl,
    )
    tracks = avatar_tracks(session, template, version, edl)
    plates = video_avatars.name_plates(session, template)
    text_png = text_png_for(
        template,
        work_dir=out_dir,
        width=template.canvas_w or 1920,
        height=template.canvas_h or 1080,
        visible_avatars={track.name for track in tracks} or None,
        avatar_names=plates,
    )
    target = out_dir / (
        f"still-{at_ms:09d}-"
        f"{_still_digest(resolved_style, at_ms, plan, tracks, template, burn_subtitles, plates)}.png"
    )
    canvas_w = template.canvas_w or 1920
    canvas_h = template.canvas_h or 1080
    # The still draws exactly one stretch, and it goes to the same layer the export would put
    # it on (§19.26): a 全屏 block above the avatars and the text, everything else below them.
    # ``None`` for both chains means "this frame has nothing to draw", which is a plate gap.
    box_chain: box_track.BoxChain | None = None
    box_segment: box_track.BoxSegment | None = None
    box_source: Path | None = None
    top_chain: box_track.BoxChain | None = None
    top_segment: box_track.BoxSegment | None = None
    top_source: Path | None = None
    if not plan.empty:
        covered = video_box.covering(plan, at_ms=at_ms)
        if covered is not None:
            found, source, offset = covered
            # ``found.input_index`` indexes into the *plan's* deduplicated input list, but
            # the preview declares one input per chain. Passing it through unchanged asked
            # ffmpeg for `[5:v]` on a two-input command, which fails as
            # "Error binding filtergraph inputs/outputs: Invalid argument" — an error that
            # names neither the box nor the preview.
            single = box_track.BoxSegment(
                input_index=0,
                duration_ms=found.duration_ms,
                kind=found.kind,
                fit=found.fit,
                crop=found.crop,
                loop=found.loop,
                placement=found.placement,
            )
            full = found.placement == "full"
            chain = box_track.build_single(
                single,
                source_offset_ms=offset,
                width=canvas_w if full else plan.width,
                height=canvas_h if full else plan.height,
                x=0 if full else plan.box_x,
                y=0 if full else plan.box_y,
                # Two chains in one graph must not share a label. The still graph uses the same
                # names the export does: [boxout] for the lower chain, [topout] for the
                # upper one.
                out_label="topout" if full else "boxout",
            )
            if full:
                top_chain, top_segment, top_source = chain, single, source
            else:
                box_chain, box_segment, box_source = chain, single, source

    video_media.render_still(
        settings.ffmpeg_path,
        target=target,
        at_ms=int(at_ms),
        width=template.canvas_w or 1920,
        height=template.canvas_h or 1080,
        background=_base_background(template, tracks),
        layers=_layers(template),
        box=box_chain,
        box_segment=box_segment,
        box_source=box_source,
        top=top_chain,
        top_segment=top_segment,
        top_source=top_source,
        avatars=_avatar_stills(tracks, at_ms=at_ms),
        text=None if text_png is None else video_media.Layer(path=text_png, x=0, y=0),
        subtitles=out_dir / "preview.ass" if burn_subtitles else None,
    )
    return target


def _still_digest(
    style: SubtitleStyle,
    at_ms: int,
    plan: video_box.BoxPlan,
    tracks: list[AvatarTrack],
    template: VideoTemplate,
    burn_subtitles: bool,
    avatar_names: dict[str, str] | None = None,
) -> str:
    """A short hash of everything that changes the pixels of one still.

    Only what the *frame* depends on: the style, the instant, which box segment is showing
    there, the avatar state at that instant, and the avatar bindings. Deliberately not the
    whole timeline — moving a block that is not on screen must not invalidate this picture,
    or every edit would re-render every still.
    """
    covered = video_box.covering(plan, at_ms=at_ms)
    segment = ""
    if covered is not None:
        found = covered[0]
        # ``covered[1]`` is the *file* the segment draws. Its **name** carries facts the rest of
        # this digest cannot see: a slide's PNG is named after the size and the typefaces it was
        # drawn with (§19.16), so changing a deck's font produces a new name for the same
        # instant. Without it the still kept its old name and the browser kept the old picture —
        # the exact "I changed it and the preview did not move" failure trap 24 is about.
        # Name only, not the full path: the file lives under a data dir that differs per test
        # run, and a digest that moves with the temp directory is not about the pixels.
        segment = (
            f"{found.kind}/{found.fit}/{found.placement}/{found.duration_ms}/"
            f"{found.crop}/{found.loop}/{Path(covered[1]).name}"
        )
    lit = ",".join(f"{track.name}@{int(lit_at(track.lit, at_ms))}" for track in tracks)
    parts = [
        f"{style.font_name}/{style.font_size}/{style.primary_colour}/"
        f"{style.outline_colour}/{style.outline}/{style.shadow}/"
        f"{style.margin_v}/{style.margin_l}/{style.margin_r}/{int(style.bold)}",
        str(int(at_ms)),
        str(int(burn_subtitles)),
        template.bindings_json,
        # The title / episode number / avatar names are pixels too. Without this, typing a
        # new title left every still byte-identical and the browser kept showing the old
        # frame under an unchanged URL (§19.11 trap 24).
        video_edits.signature(template),
        # Which names get drawn depends on which slots are bound, so binding alone changes the
        # text layer. ``lit`` below has the same information but only for the slots that have a
        # track — a slot whose name is hidden because it has *no* track is invisible there.
        ",".join(sorted(track.name for track in tracks)) or "-",
        # And what those names **say** is the bound speaker's name (round 99): renaming 呈AA
        # moves no text edit, so without this the still was byte-identical under the same URL.
        json.dumps(avatar_names or {}, sort_keys=True, ensure_ascii=False),
        segment,
        lit,
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


# --- export -------------------------------------------------------------


def start_export(
    db: object,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    options: ExportOptions | None = None,
) -> Render:
    """Record the job and hand it to the worker pool.

    Returns the same ``Render`` row type the audio exporter uses, so the UI polls one
    endpoint shape for both and the progress bar code is not duplicated.
    """
    from animatter.db import Database

    assert isinstance(db, Database)
    resolved = options or ExportOptions()

    with db.session() as session:
        prepared = prepare(session, settings, project_id, version_id)
        record = Render(
            id=new_id("vren"),
            project_id=project_id,
            version_id=version_id,
            # The video's identity is its EDL *and* its timeline *and* its template;
            # hashing only the EDL would let a cached export survive a block move.
            edl_hash=_video_signature(prepared, resolved),
            out_path="",
            duration_ms=prepared.duration_ms,
            status="queued",
            progress=0.0,
            created_at=utcnow_iso(),
        )
        session.add(record)
        session.commit()
        render_id = record.id

    _futures[render_id] = _executor.submit(
        _run, db, settings, render_id, project_id, version_id, resolved
    )
    with db.session() as session:
        started = session.get(Render, render_id)
        assert started is not None
        log.info("video.queued", render_id=render_id, duration_ms=started.duration_ms)
        return started


def _video_signature(prepared: PreparedVideo, options: ExportOptions) -> str:
    """A hash over everything that can change the pixels or the audio.

    ``bindings_json`` is in here because the avatar tracks are: which speaker a slot shows
    decides whether that slot ever goes dim, so re-binding is a pixel change. It was missing
    while ``start_export`` queued a fresh encode every time — the hash was decorative, so
    nothing broke. It stops being decorative the moment an export is reused, and then the
    bug would be "I changed the avatar binding and the film did not change", with no error.
    """
    style = options.style()
    parts = [
        prepared.template.source_sha256,
        prepared.template.box_rect,
        prepared.template.bindings_json,
        # Text and avatar-photo overrides (§19.12). Reusing an export across an edit
        # would otherwise hand back a film with the old title, silently.
        video_edits.signature(prepared.template),
        ",".join(sorted(track.name for track in prepared.avatars)) or "-",
        json.dumps(prepared.avatar_names, sort_keys=True, ensure_ascii=False),
        str(prepared.duration_ms),
        hashlib.sha256(
            ";".join(
                f"{item.start_ms}-{item.end_ms}:{item.kind}:{item.fit}:"
                f"{item.asset_id or ''}:{item.deck_id or ''}:{item.gallery_json}:{item.motion}:"
                # The mute flag changes what the film sounds like, so an export cached
                # across a click on 「M」 would hand back the previous audio silently.
                f"{'muted' if item.muted else ('solo' if item.solo else 'audible')}"
                for item in prepared.items
            ).encode()
        ).hexdigest(),
        ";".join(sorted(track.name for track in prepared.avatars)),
        f"{style.font_name}/{style.font_size}/{style.primary_colour}/"
        f"{style.margin_v}/{style.outline}/{options.burn_subtitles}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _run(
    db: object,
    settings: Settings,
    render_id: str,
    project_id: str,
    version_id: str,
    options: ExportOptions,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)
    try:
        with _lock_for(project_id):
            _encode(db, settings, render_id, project_id, version_id, options)
    except AppError as exc:
        _finish(db, render_id, status="failed", error=f"{exc.message}：{exc.hint}")
    except Exception as exc:
        log.exception("video.crashed", render_id=render_id)
        _finish(db, render_id, status="failed", error=str(exc))
    finally:
        with _locks_guard:
            _futures.pop(render_id, None)


def _chain_for(
    plan: video_box.BoxPlan,
    segments: list[box_track.BoxSegment],
    sources: list[Path],
    *,
    out_label: str,
    start_ms: int = 0,
    stills: dict[int, Path] | None = None,
) -> tuple[box_track.BoxChain, list[box_track.BoxSegment], list[Path]]:
    """One chunk's material chain, with its inputs renumbered and its files narrowed (§19.27).

    A chunk declares only the files it actually shows, so its ``-i`` list is as short as its
    segment list. That is the whole point: the memory cost tracked the number of *inputs*, and
    a chunk of six segments needs at most six.

    ``start_ms`` is where the chunk begins on the output timeline. The chain counts each
    segment's frames from the **film's** frame grid, not from the chunk's own zero, or the
    chunk's internal boundaries land on the wrong side of a half-frame and the join between two
    chunks shows a one-frame jump (measured: the sample film's picture started one frame early
    in a three-chunk export).

    Returns the **renumbered** segments and their files, not the plan's: the renderer emits its
    ``-i`` list by indexing ``sources[segment.input_index]``, so handing it the plan-wide indices
    alongside a narrowed file list reads past the end (and did, the first time).

    ``stills`` maps a position **within this slice** to an already-composited PNG (§19.28); those
    segments are swapped for ``precomposited`` ones by :func:`box_track.with_still_sources` before
    the chain is built, so the chain never sees the original file.
    """
    renumbered, ordered = video_chunks.rebase(segments, sources)
    if stills:
        renumbered, ordered = box_track.with_still_sources(renumbered, ordered, stills)
    if plan.split is None:
        chain = box_track.build_chain(
            renumbered,
            width=plan.width,
            height=plan.height,
            x=plan.box_x,
            y=plan.box_y,
            out_label=out_label,
            start_ms=start_ms,
        )
    else:
        chain = box_track.build_canvas_chain(
            renumbered,
            canvas_w=plan.canvas_w,
            canvas_h=plan.canvas_h,
            box_w=plan.width,
            box_h=plan.height,
            box_x=plan.box_x,
            box_y=plan.box_y,
            out_label=out_label,
            start_ms=start_ms,
        )
    return chain, renumbered, ordered


def _needs_top_chain(segments: list[box_track.BoxSegment]) -> bool:
    """Whether the upper chain of a chunk draws anything at all (§19.28).

    The split plan gives every chunk two chains: the lower one draws box blocks and placeholders,
    the upper one draws 全屏 blocks and placeholders (§19.26). A chunk with no 全屏 block
    therefore has an upper chain made entirely of ``color=black@0`` — a full-canvas RGBA branch
    that costs framesync queues and draws nothing. Measured on chunk 8 of 费那奇MV: skipping it
    is pixel-identical (frame-by-frame PSNR ``inf``) and 23 % faster (267.4 s -> 206.4 s).

    Pure and module-level so the decision can be tested without a session or an encode.
    """
    return any(segment.kind != box_track.KIND_TRANSPARENT for segment in segments)


def _composite_stills(
    settings: Settings,
    plan: video_box.BoxPlan,
    segments: list[box_track.BoxSegment],
    sources: list[Path],
    *,
    work_dir: Path,
    tag: str,
) -> dict[int, Path]:
    """Composite every **still** segment of one chunk into a PNG (§19.28).

    Returns position-in-slice -> PNG, which :func:`_chain_for` hands to
    ``box_track.with_still_sources``. Video segments are left out: every frame of one is
    different, so it has to stay on the live chain.

    Placeholders are left out too. They are a ``color`` source with no file (§19.26), and
    rendering one would turn a zero-cost filter into a 1920x1080 transparent PNG decoded 24 times
    a second.

    The PNG is the chain's own target size: the canvas when the plan is split (RGBA, padded at the
    box), the box rect otherwise (opaque, drawn straight onto the plate). Getting that wrong does
    not error — it renders a still scaled into the wrong rectangle.

    Measured on chunk 2 of 费那奇MV (95 s, 6 stills) through this exact path: 65.2 s / 4.7-6.2 GB
    before, **33.0 s / 3.03 GB** after, plus 1.6 s for the six composites. Frame-by-frame PSNR
    against the live chain: mean 48.97 dB, minimum 46.97 dB, all 2286 frames >=45 dB.
    """
    renumbered, _ordered = video_chunks.rebase(segments, sources)
    split = plan.split is not None
    if split:
        # The split chain is canvas-sized RGBA with the box padding baked in, so the piece has to
        # be canvas-sized too or the transparent margin would be lost.
        width, height = plan.canvas_w, plan.canvas_h
        box_x, box_y = plan.box_x, plan.box_y
    else:
        # The opaque chain is box-sized and drawn straight at the box rect: a canvas-sized piece
        # here would be scaled again by the overlay, which is exactly the double fit the
        # `precomposited` flag exists to avoid. Padding to the same size at 0:0 is a no-op.
        width, height = plan.width, plan.height
        box_x, box_y = 0, 0
    out: dict[int, Path] = {}
    for position, segment in enumerate(renumbered):
        if segment.kind in (box_track.KIND_VIDEO, box_track.KIND_TRANSPARENT):
            continue
        target = work_dir / f"still-{tag}-{position:02d}.png"
        if not target.is_file():
            video_media.composite_still(
                settings.ffmpeg_path,
                target=target,
                width=width,
                height=height,
                segment=segment,
                source=_ordered[segment.input_index],
                box_w=plan.width,
                box_h=plan.height,
                box_x=box_x,
                box_y=box_y,
            )
        out[position] = target
    return out


def _render_picture(
    db: object,
    settings: Settings,
    *,
    render_id: str,
    prepared: PreparedVideo,
    plan: video_box.BoxPlan,
    cues: list[AssCue],
    style: SubtitleStyle,
    burn_subtitles: bool,
    work_dir: Path,
    stem: str,
    target: Path,
    report: Callable[[float], None],
) -> None:
    """Encode the whole picture track, **one chunk of the timeline per ffmpeg process** (§19.27).

    The reason is memory, not speed. The material chain puts one branch per segment into the
    filtergraph, ffmpeg is push-based, and every branch that is not yet current still receives
    frames — so peak RSS tracks the number of branches and not the frame size. Measured on the
    68-segment / 3696 s 费那奇MV project: 6 branches 1.2 GB, 20 branches 2.7 GB, 40 branches
    5.2 GB, 68 branches 9.2-10.1 GB, where a 16 GB machine runs out and ffmpeg exits -12. At 720p
    the peak was unchanged (8.8 GB), which is what ruled out "just export smaller".

    Chunks are joined with ``-c copy``, so the film costs one extra read/write of its own bytes
    and no quality. Boundaries are segment boundaries and each chunk is asked for an exact frame
    count, so the join is frame-exact: verified 88702 frames == 3695.93 s @ 24 fps, and a chunk
    rendered losslessly matched ``preview_still`` to within 1/255.

    A film short enough to be one chunk takes the same path with a rename instead of a concat,
    so there is only ever one code path and the tests keep exercising it.
    """
    canvas_w = prepared.template.canvas_w or 1920
    canvas_h = prepared.template.canvas_h or 1080
    layers = _layers(prepared.template)
    text_png = text_png_for(
        prepared.template,
        work_dir=work_dir,
        width=canvas_w,
        height=canvas_h,
        visible_avatars={track.name for track in prepared.avatars} or None,
        avatar_names=prepared.avatar_names,
    )

    if plan.empty:
        # A film with a background and subtitles but no material at all. `whole` is the one
        # chunk, and it has no chain — the unchunked graph, untouched.
        chunks = video_chunks.whole(prepared.duration_ms)
        lower: list[box_track.BoxSegment] = []
        lower_sources: list[Path] = []
        upper: list[box_track.BoxSegment] = []
        upper_sources: list[Path] = []
    else:
        lower = list(plan.segments if plan.split is None else plan.split.lower)
        lower_sources = list(plan.sources if plan.split is None else plan.split.lower_sources)
        upper = list(plan.split.upper) if plan.split is not None else []
        upper_sources = list(plan.split.upper_sources) if plan.split is not None else []
        chunks = video_chunks.split(
            lower,
            max_segments=settings.video_chunk_segments,
            max_ms=settings.video_chunk_seconds * 1000,
        )

    chunk_dir = work_dir / f"{stem}-chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for stale in chunk_dir.glob("*.mp4"):
        stale.unlink()
    # A previous export that died mid-chunk leaves its still composites behind, and they are
    # named by chunk index, so a re-export of the *same* film would happily reuse them. Delete
    # them rather than trust them: they are one ffmpeg call each to rebuild (§19.28).
    for stale in chunk_dir.glob("still-*.png"):
        stale.unlink()

    def render_one(index: int, chunk: video_chunks.Chunk) -> Path:
        piece = chunk_dir / f"chunk-{index:04d}.mp4"
        box_chain = top_chain = None
        box_parts: list[box_track.BoxSegment] = []
        box_inputs: list[Path] = []
        top_parts: list[box_track.BoxSegment] = []
        top_inputs: list[Path] = []
        if not plan.empty:
            stills = (
                _composite_stills(
                    settings,
                    plan,
                    lower[chunk.first : chunk.last],
                    lower_sources,
                    work_dir=chunk_dir,
                    tag=f"chunk-{index:04d}",
                )
                if settings.video_still_composite
                else {}
            )
            # The renumbered segments, not the plan's slice: the renderer indexes
            # `sources[segment.input_index]`, and a chunk's file list is narrowed to its own.
            box_chain, box_parts, box_inputs = _chain_for(
                plan,
                lower[chunk.first : chunk.last],
                lower_sources,
                out_label="boxout",
                start_ms=chunk.start_ms,
                stills=stills,
            )
            # A top chain of nothing but `color=black@0` placeholders is pure overhead: the
            # lower chain already shows the plate everywhere a 全屏 block is not, and this
            # film has exactly one such block, so 13 of its 14 chunks would pay for a
            # full-canvas RGBA branch that draws nothing. Measured on chunk 8: skipping it is
            # pixel-identical (PSNR inf) and 23 % faster (267.4 s -> 206.4 s).
            if upper and _needs_top_chain(upper[chunk.first : chunk.last]):
                top_chain, top_parts, top_inputs = _chain_for(
                    plan,
                    upper[chunk.first : chunk.last],
                    upper_sources,
                    out_label="topout",
                    start_ms=chunk.start_ms,
                )

        subtitles: Path | None = None
        if burn_subtitles:
            # Re-based to chunk-local time. Reusing one whole-film ASS would draw every cue on
            # every chunk, because libass decides from the frame's PTS and every chunk is at 0.
            subtitles = write_ass(
                piece.with_suffix(".ass"),
                video_chunks.slice_cues(cues, chunk),
                style=style,
            )

        # A **short track for this chunk** (see `_avatar_layers`): seeking the whole-film track
        # into the chunk truncated it and the avatar vanished for the rest of the chunk.
        avatars = _avatar_layers(
            settings,
            prepared.avatars,
            duration_ms=prepared.duration_ms,
            work_dir=work_dir,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
            tag=f"-{index:04d}",
        )

        video_media.render(
            settings.ffmpeg_path,
            target=piece,
            duration_ms=chunk.duration_ms,
            width=canvas_w,
            height=canvas_h,
            background=prepared.background_path,
            layers=layers,
            box=box_chain,
            box_segments=box_parts or None,
            box_sources=box_inputs or None,
            top=top_chain,
            top_segments=top_parts or None,
            top_sources=top_inputs or None,
            avatars=avatars,
            text=None if text_png is None else video_media.Layer(path=text_png, x=0, y=0),
            subtitles=subtitles,
            start_ms=chunk.start_ms,
            frames=chunk.frames(video_media.FPS),
            filter_threads=settings.video_filter_threads,
        )
        return piece

    pieces: list[Path] = [chunk_dir / f"chunk-{index:04d}.mp4" for index in range(len(chunks))]
    if len(chunks) == 1:
        pieces[0] = render_one(0, chunks[0])
    else:
        log.info(
            "video.chunked",
            render_id=render_id,
            chunks=len(chunks),
            segments=len(lower),
            workers=settings.video_chunk_workers,
        )
        done = 0
        progress_lock = threading.Lock()
        with ThreadPoolExecutor(
            max_workers=max(1, settings.video_chunk_workers),
            thread_name_prefix=f"chunk-{render_id}",
        ) as pool:
            futures = {
                pool.submit(render_one, index, chunk): index
                for index, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                pieces[futures[future]] = future.result()
                with progress_lock:
                    done += 1
                    report(0.9 * done / len(chunks))

    if len(pieces) == 1:
        # One chunk: a rename, not a stream copy. Copying a 1 GB file to itself would be the
        # most expensive thing in the export.
        os.replace(pieces[0], target)
        piece_dir = chunk_dir
    else:
        video_media.concat(settings.ffmpeg_path, parts=pieces, target=target)
        piece_dir = chunk_dir
    # The chunks are ~1 GB of intermediate files; leaving them would quietly double the film's
    # disk cost every export.
    for leftover in piece_dir.glob("chunk-*"):
        leftover.unlink(missing_ok=True)
    # And the per-chunk still composites (§19.28): a handful of 1080p PNGs per chunk, regenerated
    # from scratch every export and named by chunk, so nothing ever reuses one.
    for leftover in piece_dir.glob("still-*"):
        leftover.unlink(missing_ok=True)
    # Same for the per-chunk avatar tracks: 14 chunks x 2 slots x ~3 MB is small, but they are
    # regenerated on every export and are named by chunk, so nothing ever reuses them. Left
    # behind they accumulate for the life of the project.
    for stale_track in work_dir.glob("avatar-*-[0-9][0-9][0-9][0-9].mov"):
        stale_track.unlink(missing_ok=True)


def _encode(
    db: object,
    settings: Settings,
    render_id: str,
    project_id: str,
    version_id: str,
    options: ExportOptions,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)

    with db.session() as session:
        prepared = prepare(session, settings, project_id, version_id)
        record = session.get(Render, render_id)
        if record is None:
            raise AppError("render_not_found", "导出任务不存在", status=404)

        # Resolved **here**, not in `prepare`: an empty edit has no prior export, so this is
        # where the audio gets made (§19.22), and making it inside the request that queues the
        # job would block the UI for the length of the episode.
        audio = _audio_for(
            session,
            settings,
            clipping_service.get_version(session, version_id),
            lossless=options.lossless_audio,
        )

        out = output_dir(settings, project_id)
        stem = f"{version_id}-{record.edl_hash[:12]}"
        silent = out / f"{stem}-silent.mp4"
        final = out / f"{stem}.mp4"
        record.out_path = str(final)
        record.status = "running"
        record.progress = 0.0

    def report(fraction: float) -> None:
        with db.session() as progress_session:
            row = progress_session.get(Render, render_id)
            if row is not None and row.status == "running":
                # Encoding is ~75 % of the wall clock and muxing is ~1 %, but the
                # split is not fixed; scaling to 0.9 keeps the bar from sitting at
                # 100 % while the mux runs.
                row.progress = round(min(0.9, fraction * 0.9), 4)

    with db.session() as session:
        plan = _box(
            session,
            settings=settings,
            template=prepared.template,
            box_rect=prepared.box_rect,
            items=prepared.items,
            edl=prepared.edl,
        )
        # The cues themselves, not one whole-film ASS: each chunk burns its own re-based copy
        # (§19.27). A single ASS would put every cue in every chunk, because the burn is
        # relative to the frame's PTS and every chunk starts at 0.
        cues = _cues_for(session, version_id, prepared.edl) if options.burn_subtitles else []
    if options.burn_subtitles and not cues:
        # Same refusal as `_ass_for`, kept here so the error lands before any encoding.
        raise AppError(
            "no_subtitle_cues",
            "这个版本没有可烧入的字幕",
            hint="先完成转写并确认字幕内容。",
        )

    # The text PNG and the avatar tracks are built by `_render_picture`, once per export: it is
    # the only consumer, and building them twice would re-render the same file.
    _render_picture(
        db,
        settings,
        render_id=render_id,
        prepared=prepared,
        plan=plan,
        cues=cues,
        style=options.style(),
        burn_subtitles=options.burn_subtitles,
        work_dir=out,
        stem=stem,
        target=silent,
        report=report,
    )
    report(0.95)

    result = video_media.mux(
        settings.ffmpeg_path,
        video=silent,
        audio=audio,
        target=final,
        # Empty unless the user unmuted something (§19.19), which is what keeps the silent
        # case on the `-c copy` path it was designed around.
        mix=[
            video_media.AudioMix(
                start_ms=item.start_ms, end_ms=item.end_ms, path=item.path, solo=item.solo
            )
            for item in plan.audible
        ],
    )
    silent.unlink(missing_ok=True)

    _finish(db, render_id, status="done", duration_ms=result.duration_ms or prepared.duration_ms)
    log.info("video.done", render_id=render_id, path=str(final), duration_ms=result.duration_ms)


def _finish(
    db: object,
    render_id: str,
    *,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)
    with db.session() as session:
        row = session.get(Render, render_id)
        if row is None:
            return
        row.status = status
        row.error = error
        if status == "done":
            row.progress = 1.0
        if duration_ms is not None:
            row.duration_ms = duration_ms


def fail_orphaned(db: object) -> int:
    """Mark video exports left running by a previous process as failed."""
    from animatter.db import Database

    assert isinstance(db, Database)
    with db.session() as session:
        rows = session.scalars(
            select(Render).where(Render.status.in_(_ACTIVE), Render.id.startswith("vren"))
        ).all()
        for row in rows:
            row.status = "failed"
            row.error = "服务重启，视频导出中断；请重新导出。"
        count = len(rows)
    if count:
        log.warning("video.orphans_failed", count=count)
    return count
