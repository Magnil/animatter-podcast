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
3. **Silent video.** One ffmpeg, one filtergraph in a script file (§19.11.1).
4. **Mux.** ``-c copy``; measured 0.24 s per 60 s.

Still preview is a separate, request-time path: ~200 ms per frame, so the UI can call
it as the playhead moves without a job row.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import Edl
from animatter.core.video_avatar import Interval, lit_at, track_pieces
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import avatar_track, box_track
from animatter.media import text_overlay as overlay_media
from animatter.media import video as video_media
from animatter.media.subtitle_ass import AssCue, SubtitleStyle, write_ass
from animatter.models import Render, TranscriptVersion, VideoItem, VideoTemplate
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
    audio_path: Path
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
    """The already-rendered audio for this exact EDL, or a fresh render of it.

    Reusing the audio is the whole point of the ``-c copy`` mux: re-encoding it here
    would cost another ten-plus minutes and produce a file the user has not approved.
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

    raise AppError(
        "audio_not_rendered",
        "还没有这个版本的成片音频",
        hint="先在「导出」里导出成片音频，再导出视频。视频会直接复用那条音轨。",
        status=409,
    )


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
    audio = _audio_for(session, settings, version, lossless=lossless_audio)
    items = video_service.list_items(session, project_id)

    tracks = avatar_tracks(session, template, version, edl)
    return PreparedVideo(
        template=template,
        box_rect=template_service.box_rect_of(template),
        items=items,
        edl=edl,
        audio_path=audio,
        duration_ms=edl.output_duration_ms,
        avatars=tracks,
        avatar_names=video_avatars.name_plates(session, template),
    )


def _ass_for(
    session: Session,
    version_id: str,
    edl: Edl,
    style: SubtitleStyle,
    target: Path,
) -> Path:
    """Build the burned-in subtitle file in output time."""
    time_map = edl.time_map() if edl.removed_ms else None
    cues, _issues = subtitle_service.build_cues(session, version_id, time_map=time_map)
    ass_cues = [AssCue(start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text) for cue in cues]
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
) -> list[video_media.AvatarLayer]:
    """Render one lit/dim track per bound slot, ready to overlay.

    Measured 1.5 s and 55 MB per 600 s of track, so the cost is small next to the encode
    itself. Rendering them here rather than in ``prepare`` keeps the request that queues an
    export fast: the user is watching a progress bar either way.
    """
    layers: list[video_media.AvatarLayer] = []
    for track in tracks:
        sequence = track_pieces(track.lit, total_ms=duration_ms)
        pieces = avatar_track.pieces_of(
            lit=[track.lit_path], dim=track.dim_path, sequence=sequence
        )
        target = work_dir / f"avatar-{_slug(track.name)}.mov"
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
    box_chain: box_track.BoxChain | None = None
    box_segment: box_track.BoxSegment | None = None
    box_source: Path | None = None
    if not plan.empty:
        covered = video_box.covering(plan, at_ms=at_ms)
        if covered is not None:
            found, box_source, offset = covered
            # ``found.input_index`` indexes into the *plan's* deduplicated input list, but
            # the preview only declares one box input. Passing it through unchanged asked
            # ffmpeg for `[5:v]` on a two-input command, which fails as
            # "Error binding filtergraph inputs/outputs: Invalid argument" — an error that
            # names neither the box nor the preview.
            box_segment = box_track.BoxSegment(
                input_index=0,
                duration_ms=found.duration_ms,
                kind=found.kind,
                fit=found.fit,
                crop=found.crop,
                loop=found.loop,
            )
            box_chain = box_track.build_single(
                box_segment,
                source_offset_ms=offset,
                width=plan.width,
                height=plan.height,
                x=plan.box_x,
                y=plan.box_y,
            )

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
        segment = (
            f"{found.kind}/{found.fit}/{found.duration_ms}/"
            f"{found.crop}/{found.loop}/{covered[1]}"
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
                f"{item.asset_id or ''}:{item.deck_id or ''}:{item.gallery_json}:{item.motion}"
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
        prepared = prepare(
            session, settings, project_id, version_id, lossless_audio=options.lossless_audio
        )
        record = session.get(Render, render_id)
        if record is None:
            raise AppError("render_not_found", "导出任务不存在", status=404)

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

    subtitles: Path | None = None
    if options.burn_subtitles:
        with db.session() as session:
            subtitles = _ass_for(
                session, version_id, prepared.edl, options.style(), out / f"{stem}.ass"
            )

    with db.session() as session:
        plan = _box(
            session,
            settings=settings,
            template=prepared.template,
            box_rect=prepared.box_rect,
            items=prepared.items,
            edl=prepared.edl,
        )
        box_chain = (
            None
            if plan.empty
            else box_track.build_chain(
                plan.segments,
                width=plan.width,
                height=plan.height,
                x=plan.box_x,
                y=plan.box_y,
            )
        )

    text_png = text_png_for(
        prepared.template,
        work_dir=out,
        width=prepared.template.canvas_w or 1920,
        height=prepared.template.canvas_h or 1080,
        visible_avatars={track.name for track in prepared.avatars} or None,
        avatar_names=prepared.avatar_names,
    )

    video_media.render(
        settings.ffmpeg_path,
        target=silent,
        duration_ms=prepared.duration_ms,
        width=prepared.template.canvas_w or 1920,
        height=prepared.template.canvas_h or 1080,
        background=prepared.background_path,
        layers=_layers(prepared.template),
        box=box_chain,
        box_segments=None if plan.empty else plan.segments,
        box_sources=None if plan.empty else plan.sources,
        avatars=_avatar_layers(
            settings,
            prepared.avatars,
            duration_ms=prepared.duration_ms,
            work_dir=out,
        ),
        text=None if text_png is None else video_media.Layer(path=text_png, x=0, y=0),
        subtitles=subtitles,
    )
    report(0.95)

    result = video_media.mux(
        settings.ffmpeg_path, video=silent, audio=prepared.audio_path, target=final
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
