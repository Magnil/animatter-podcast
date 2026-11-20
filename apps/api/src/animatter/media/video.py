"""Video rendering: filtergraph assembly and the two encode paths (§19.5).

Three measured facts shape this module.

**One ffmpeg, one filtergraph, written to a file.** The graph for a real episode is
big: a 1-hour film with avatars and a box is thousands of characters, and Windows
caps a command line at 32,767. Measured: 12,307 characters runs, 38,187 fails with
``The filename or extension is too long`` — which reads like a path bug and is not.
``-filter_complex_script`` takes the graph as an *input* option (after the ``-i``s);
``-/filter_complex <file>`` also works but only as a *global* option, i.e. before the
first ``-i``. Putting it in the wrong place yields ``Invalid argument``.

**Practically all the time is x264.** Measured per 60 s of output: empty background
10.95 s, background + box + burned subtitles + four avatar tracks 14.19 s. So
compositing is ~3 s and encoding is ~11 s, which means optimising the graph is
pointless and ``preset``/resolution are the only real levers. ``veryfast`` costs 15 %
more than ``ultrafast`` and produces a third of the size.

**The output is deterministic.** Two runs of the same command produced the identical
SHA-256, which is what makes "export twice and compare" a real workflow, and what
lets an export be cached by graph hash the way audio renders are.

Audio is **not** encoded here. The audio render already has its own EDL-hash cache,
progress reporting and worker pool; re-cutting it would burn ten-plus minutes of CPU
for a file that already exists. The last step is a ``-c copy`` mux, measured at 0.24 s
per 60 s.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media import box_track
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.media.subtitle_ass import ensure_font as ensure_font_installed

log = get_logger(__name__)

#: 1080p24 is the deliverable. 24 fps rather than 25 (round 118): the artwork and the archive
#: this film is cut against run at 24, and a 25 fps encode of 24 fps material judders on every
#: fifth frame for no benefit. The output rate is also the *only* rate the timeline uses —
#: ``box_track.FPS`` must stay equal to this one, or ``concat`` duplicates frames at every
#: material boundary.
FPS: Final[int] = 24
CRF: Final[str] = "20"
#: `veryfast` measured 9.42 s per 60 s against `ultrafast`'s 8.06 s, at a third of
#: the file size. The 15 % is not worth tripling the upload.
PRESET: Final[str] = "veryfast"

ProgressFn = Callable[[float], None]


@dataclass(frozen=True)
class VideoResult:
    path: Path
    duration_ms: int
    frames: int


@dataclass(frozen=True)
class Layer:
    """One layer composited on top of the background.

    ``width``/``height`` default to the canvas, which is right for a full-frame artwork
    layer (the frame stroke). The box content track is **not** full-frame — it is sized
    to the box rect and positioned at it — so scaling it to the canvas would smear one
    1440×810 image across 1920×1080 and push it off its own edge.

    ``is_video`` picks the input declaration: a still is ``-loop 1 -t N``, a rendered
    track is a real video and must be neither looped nor truncated at the input.
    """

    path: Path
    x: int = 0
    y: int = 0
    width: int | None = None
    height: int | None = None
    is_video: bool = False


@dataclass(frozen=True)
class AvatarLayer:
    """One avatar's lit/dim track, placed at its slot (P3).

    Unlike :class:`Layer` this is always a **video** input: the track was rendered by
    ``media/avatar_track`` and is exactly as long as the film, so it must be neither looped
    nor truncated at the input. ``eof_action=pass`` on the overlay is the safety net — if
    the track ever ends a frame early, the frame underneath shows instead of the last avatar
    state freezing on screen for the rest of the film.
    """

    path: Path
    x: int
    y: int
    width: int
    height: int
    #: ``False`` for the still preview, which shows one lit-or-dim PNG per slot rather
    #: than paying for a track. It is the same picture at that instant, and a still costs
    #: ~200 ms against 1.5 s per 600 s of track.
    is_video: bool = True


def escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use inside a filtergraph option.

    Inside a filter argument ``:`` separates options and ``\\`` escapes, so a Windows
    path ``C:\\x\\sub.ass`` must become ``C\\:/x/sub.ass``. Forward slashes are
    accepted by ffmpeg on every platform and avoid having to escape ``\\`` as well.

    Getting this wrong is invisible until ffmpeg errors with something like
    ``Unable to parse option value`` pointing at a path that looks fine.
    """
    text = path.as_posix()
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_filtergraph(
    *,
    width: int,
    height: int,
    layers: list[Layer],
    subtitle_path: Path | None,
    offset_ms: int = 0,
    box: box_track.BoxChain | None = None,
    top: box_track.BoxChain | None = None,
    avatars: list[AvatarLayer] | None = None,
    text: Layer | None = None,
    out_label: str = "vout",
) -> str:
    """Assemble the video filtergraph.

    ``[0:v]`` is the compiled background. Every other input is a still or a
    pre-generated track overlaid in order; the subtitle burn is last so text is never
    covered by artwork.

    ``box`` is the media-box content chain (§19.5). It is overlaid **before** the artwork
    layers so the frame stroke sits on top of the picture, and after the background so the
    picture covers the plate. Its inputs come last, which is why it needs to know how many
    inputs came before it — see ``input_base``.

    ``top`` is the 全屏 half of the material chain (§19.26). It goes **after** ``text`` and
    before the subtitles: a full-frame block covers the whole canvas, so an avatar or a title
    left on top of it would look like the block failed to render. The lower ``box`` chain is
    the other half — the same timeline with the full-frame stretches replaced by transparent
    placeholders — so a film that mixes both still has one consistent timeline, drawn in two
    passes around the avatars and the text.

    ``avatars`` are P3's lit/dim tracks. They are overlaid **after** the box and before the
    subtitles: an avatar sits beside the box rather than inside it, so the order only
    matters for a template that overlaps them, and "panel in front of the slide" is the
    reading that matches how the PSD stacks the two groups. Their inputs therefore come
    after the box's, which is why the box keeps its ``1 + len(layers)`` base.

    The ``setsar=1`` on each branch is not cosmetic: the compiled PNGs carry no pixel
    aspect metadata, and a mismatch makes the ``overlay`` filter silently scale the
    layer, so the box lands a few pixels off from where the confirm screen showed it.

    ``text`` is the editable-text overlay (§19.12), applied **after** the avatars and before
    the subtitles. That position is the whole reason it is a separate argument rather than one
    more entry in ``layers``: an avatar's own rect contains its name plate, so a text layer
    drawn first is covered by the avatar PNG and the names vanish. Measured on the sample —
    group 头像1 starts at y=200 and its name sits at y=202, inside it.

    ``offset_ms`` exists for the still preview. A still at 6 s is a *frame from 6 s*,
    and ``ass`` decides what to draw from the frame's timestamp — so asking ffmpeg to
    seek instead produces a frame whose PTS is 0 and therefore no subtitle at all.
    Measured: seeking gave 0 differing pixels against a subtitle-free render, while
    shifting PTS by the same offset gave a cue at the expected place. So the offset
    is applied here, to the picture, rather than by seeking the input.
    """
    parts: list[str] = []
    # ``PTS`` is in *timebase units*, not milliseconds: with a 25 fps base,
    # ``PTS+6000/TB`` means 6000 seconds. Writing the offset in seconds is the only
    # spelling that means what it says, and getting it wrong is silent — the frame
    # simply lands outside every cue and no subtitle is drawn.
    shift = f",setpts=PTS+{offset_ms / 1000:.3f}/TB" if offset_ms else ""
    current = "0:v"

    if shift:
        parts.append(f"[0:v]scale={width}:{height},setsar=1{shift}[base0]")
        current = "base0"

    for index, layer in enumerate(layers, start=1):
        # A video track is already offset by the caller (it starts at the right instant);
        # shifting its PTS again would move the picture away from the audio.
        layer_shift = "" if layer.is_video else shift
        layer_w = layer.width if layer.width is not None else width
        layer_h = layer.height if layer.height is not None else height
        parts.append(
            f"[{current}]scale={width}:{height},setsar=1[base{index}];"
            f"[{index}:v]scale={layer_w}:{layer_h},setsar=1{layer_shift}[img{index}];"
            f"[base{index}][img{index}]overlay=x={layer.x}:y={layer.y}:"
            f"eof_action={'pass' if layer.is_video else 'repeat'}:format=auto[{label_of(index)}]"
        )
        current = label_of(index)

    if box is not None:
        # The chain's own inputs start after the background and the artwork layers.
        parts.append(box.text_for(input_base=1 + len(layers)))
        parts.append(
            f"[{current}][boxout]overlay=x={box.x}:y={box.y}:format=auto[withbox]"
        )
        current = "withbox"

    for position, avatar in enumerate(avatars or []):
        # Input numbers are 1-based: background, then every layer, then the box's distinct
        # sources. Derived rather than passed so the two orders cannot drift apart.
        input_no = 1 + len(layers) + (box.input_count if box is not None else 0) + position
        label = f"av{position}"
        avatar_shift = "" if avatar.is_video else shift
        parts.append(
            f"[{current}]scale={width}:{height},setsar=1[{label}base];"
            f"[{input_no}:v]scale={avatar.width}:{avatar.height},setsar=1{avatar_shift}"
            f"[{label}img];"
            f"[{label}base][{label}img]overlay=x={avatar.x}:y={avatar.y}:"
            f"eof_action={'pass' if avatar.is_video else 'repeat'}:format=auto[{label}]"
        )
        current = label

    if text is not None:
        # Its input number follows the avatars', for the same reason theirs follows the box's.
        text_input = (
            1
            + len(layers)
            + (box.input_count if box is not None else 0)
            + len(avatars or [])
        )
        parts.append(
            f"[{current}]scale={width}:{height},setsar=1[textbase];"
            f"[{text_input}:v]scale={text.width or width}:{text.height or height},setsar=1"
            f"{'' if text.is_video else shift}[textimg];"
            f"[textbase][textimg]overlay=x={text.x}:y={text.y}:"
            f"eof_action={'pass' if text.is_video else 'repeat'}:format=auto[withtext]"
        )
        current = "withtext"

    if top is not None:
        # Input number derived, never passed: the chain's own segments start after the
        # background, the artwork, the lower box chain, the avatars and the text.
        top_input = (
            1
            + len(layers)
            + (box.input_count if box is not None else 0)
            + len(avatars or [])
            + (1 if text is not None else 0)
        )
        parts.append(top.text_for(input_base=top_input))
        # `format=auto` rather than `format=rgba`: the overlay is the last thing before the
        # subtitle burn, and a mid-graph format change costs a copy of every frame. The chain
        # already carries its own alpha, and `overlay` reads it either way.
        parts.append(f"[{current}][topout]overlay=x={top.x}:y={top.y}:format=auto[withtop]")
        current = "withtop"

    if subtitle_path is not None:
        parts.append(f"[{current}]ass=filename='{escape_filter_path(subtitle_path)}'[{out_label}]")
    elif current != out_label:
        parts.append(f"[{current}]copy[{out_label}]")

    return ";\n".join(parts)


def label_of(index: int) -> str:
    return f"l{index}"


def _run(command: list[str], *, timeout: int = 7200) -> None:
    """Run ffmpeg, surfacing its stderr when it fails.

    Same UTF-8 posture as ``media/ffmpeg._run``: never ``text=True``, because a CJK
    path raises ``UnicodeDecodeError`` in the reader thread and ``subprocess.run``
    swallows it, leaving ``None`` output and a reported success.
    """
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AppError(
            "ffmpeg_missing",
            f"找不到 {command[0]}",
            hint="安装 ffmpeg 并确保在 PATH 上，或设置 AM_FFMPEG_PATH。",
            status=503,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            "video_render_timeout",
            f"视频编码超时（>{timeout}s）",
            hint="导出这么大的片子本来就慢；先导出较短的片段确认参数没问题。",
            status=504,
        ) from exc

    if completed.returncode != 0:
        raise AppError(
            "video_render_failed",
            "视频渲染失败",
            hint=(completed.stderr or "")[-600:],
            status=500,
        )


def _chain_inputs(
    segments: list[box_track.BoxSegment],
    sources: list[Path],
    frame_counts: list[int],
) -> list[str]:
    """The `-i` declarations for one material chain, one per distinct source.

    **Placeholders are skipped.** A transparent segment (§19.26) is a `color` filter with no
    file behind it, and its `input_index` is 0 — the same number a real segment's first source
    uses. Declaring it would either ask `input_args` for a path that does not exist or emit the
    same file twice, and either way every later input (avatars, text, the other chain) shifts by
    one. This is the input-numbering half of the split; `build_canvas_chain.inputs` counts by
    the same rule and the two must agree.

    ``frame_counts`` is the chain's own per-segment frame list (``BoxChain.frames``): each input
    is declared long enough for the longest stretch that reads it, so a trim that rounds up is
    never starved by its input running out. A chain whose trim is exact would mostly not need
    this, but "mostly" is how a missing frame in the middle of a 61-minute film happens.
    """
    needs = box_track.frames_per_input(segments, frame_counts)
    out: list[str] = []
    seen: set[int] = set()
    for segment in segments:
        if segment.kind == box_track.KIND_TRANSPARENT:
            continue
        if segment.input_index in seen:
            continue
        seen.add(segment.input_index)
        out += box_track.input_args(
            segment,
            sources[segment.input_index],
            frames=needs.get(segment.input_index),
        )
    return out


def render(
    ffmpeg: str,
    *,
    target: Path,
    duration_ms: int,
    width: int,
    height: int,
    background: Path,
    layers: list[Layer],
    box: box_track.BoxChain | None = None,
    box_segments: list[box_track.BoxSegment] | None = None,
    box_sources: list[Path] | None = None,
    top: box_track.BoxChain | None = None,
    top_segments: list[box_track.BoxSegment] | None = None,
    top_sources: list[Path] | None = None,
    avatars: list[AvatarLayer] | None = None,
    text: Layer | None = None,
    subtitles: Path | None = None,
    start_ms: int = 0,
    frames: int | None = None,
    preset: str = PRESET,
    crf: str = CRF,
    filter_threads: int | None = None,
) -> VideoResult:
    """Encode the silent video track.

    Each still is declared with ``-loop 1 -t <seconds>``: without the explicit
    ``-t`` ffmpeg loops forever and the encode never ends. The duration is the
    *rendered* one, so the video is exactly as long as the cut audio.

    ``box``/``box_segments``/``box_sources`` are the media-box content (§19.5): a chain
    built by ``services/video_box.plan`` and the distinct files it reads. They are passed
    as three pieces rather than one object so this module stays free of the service layer —
    the media layer only ever sees segments and paths.

    ``avatars`` are P3's lit/dim tracks, declared as plain ``-i`` inputs in the same order
    the filtergraph expects (background, artwork, box sources, avatars).

    ``top``/``top_segments``/``top_sources`` are the 全屏 half (§19.26). Their inputs come
    **after** the text overlay, i.e. last, because that is the order the graph overlays them
    in. Keeping them a separate triple rather than one list of chains is the same choice the
    box already made: the media layer sees segments and paths, not a service-layer plan.

    ``start_ms``/``frames`` render **one chunk** of a longer film (§19.27). The graph is built
    only from the chunk's own segments (the caller narrows the plan), the avatar tracks are
    seeked to ``start_ms`` because each covers the whole film, and the output is cut to exactly
    ``frames`` frames so concatenating the chunks cannot drift. A frame count rather than a
    duration on purpose: at 24 fps one frame is 41.67 ms, not representable in milliseconds, and
    rounding per chunk would accumulate.

    ``filter_threads`` caps ``-filter_complex_threads`` (§19.28). ffmpeg defaults it to one
    slice per logical CPU and each slice carries its own framesync queue, so a 1920x1080 RGBA
    frame is queued 16 times: measured on this project's chunk 2, the default peaked at 5.6-6.1
    GB and died with ``Cannot allocate memory``, two threads at 4.71-4.82 GB, one at 3.58-3.81 GB.
    ``None`` leaves ffmpeg's own default alone, which is the still-preview path and the tests.
    """
    if duration_ms <= 0 and not frames:
        raise AppError("video_duration_invalid", "视频时长为 0", hint="先渲染音频。")
    if not background.is_file():
        raise AppError(
            "template_not_compiled",
            "模板还没编译好",
            hint="在「视频」里重新导入这份 PSD。",
            status=409,
        )
    for layer in layers:
        if not layer.path.is_file():
            raise AppError(
                "template_not_compiled",
                f"模板缺少图层文件 {layer.path.name}",
                hint="重新导入 PSD 让模板重新编译。",
                status=409,
            )
    if subtitles is not None:
        if not subtitles.is_file():
            raise AppError("subtitle_missing", "字幕文件不存在", status=404)
        ensure_font_ok(subtitles)

    target.parent.mkdir(parents=True, exist_ok=True)
    seconds = f"{frames / FPS:.6f}" if frames else f"{duration_ms / 1000:.3f}"

    graph = build_filtergraph(
        width=width,
        height=height,
        layers=layers,
        subtitle_path=subtitles,
        box=box,
        top=top,
        avatars=avatars,
        text=text,
    )
    script = target.with_suffix(target.suffix + ".filtergraph")
    script.write_text(graph, encoding="utf-8")

    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        # A *global* option, so it must come before the first `-i` like any other global.
        *(["-filter_complex_threads", str(filter_threads)] if filter_threads else []),
        "-loop",
        "1",
        "-t",
        seconds,
        "-i",
        str(background),
    ]
    for layer in layers:
        if layer.is_video:
            # A rendered track is exactly as long as the film; looping it would make
            # the encode never end and `-t` would cut it at the wrong place.
            command += ["-i", str(layer.path)]
        else:
            command += ["-loop", "1", "-t", seconds, "-i", str(layer.path)]

    if box_segments and box_sources:
        # One input per *distinct* source, in the order the chain's indices refer to.
        command += _chain_inputs(
            box_segments, box_sources, box.frames if box is not None else []
        )

    for avatar in avatars or []:
        if not avatar.path.is_file():
            raise AppError(
                "avatar_track_missing",
                f"头像轨不见了：{avatar.path.name}",
                hint="重新导出；如果反复失败，把项目里的头像位重新绑定一次。",
                status=409,
            )
        # **No `-ss` and no `-t`.** The track handed to a chunk already covers exactly that
        # chunk (`services.video_render._avatar_layers` builds a short one per chunk), so both
        # bounds are not just redundant but destructive:
        #
        # * `-ss` used to seek the whole-film track; on this sparse VFR clip it delivered 7 of
        #   298 frames.
        # * `-t` cuts the terminator frame that the concat demuxer appends to give the previous
        #   frame its display duration. Losing it makes the frame before it end immediately —
        #   measured: a 137.57 s track copied with `-t 137.573` kept 9 frames ending at 129.68 s
        #   instead of 10 ending at 137.60 s, so the avatar stopped drawing 8 s before the
        #   chunk did. `eof_action=pass` then shows the plate for the rest of the chunk, which
        #   is the "头像框消失" bug exactly.
        command += ["-i", str(avatar.path)]

    if text is not None:
        # Declared as a looping still, like every other static layer: it is one PNG for the
        # whole film, and ``-t`` keeps ffmpeg from looping it forever.
        if not text.path.is_file():
            raise AppError(
                "text_overlay_missing",
                "文字图层没生成出来",
                hint="点一次「生成静帧」重试；如果反复失败，检查字体是否还在。",
                status=409,
            )
        command += ["-loop", "1", "-t", seconds, "-i", str(text.path)]

    # The top chain's inputs are declared **after** the text, because the graph overlays it
    # after the text. Order here is the input numbering the graph assumes; reversing the two
    # would draw the text as a full-frame poster and the block as a title.
    if top_segments and top_sources:
        command += _chain_inputs(
            top_segments, top_sources, top.frames if top is not None else []
        )

    command += [
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
        "-r",
        str(FPS),
        "-fps_mode",
        "cfr",
    ]
    if frames:
        command += ["-frames:v", str(frames)]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
    ]
    if not frames:
        # A chunk is never served as-is: it gets `-c copy`-concatenated, which cannot carry a
        # moov atom that has been moved to the front. Paying for faststart on every chunk
        # would rewrite each file for nothing.
        command += ["-movflags", "+faststart"]
    command += [str(target)]

    log.info(
        "video.render",
        duration_ms=duration_ms,
        layers=len(layers),
        graph_chars=len(graph),
        subtitles=subtitles is not None,
        start_ms=start_ms,
        frames=frames,
    )
    _run(command)
    script.unlink(missing_ok=True)

    if not target.exists() or target.stat().st_size == 0:
        raise AppError("video_render_empty", "视频产出为空文件", hint="检查 ffmpeg 日志与磁盘空间。")
    return VideoResult(
        path=target,
        duration_ms=duration_ms,
        frames=frames or duration_ms * FPS // 1000,
    )


def ensure_font_ok(subtitles: Path) -> None:
    """Check the ASS's font family is installed before paying for an encode.

    Reads the family back out of the file rather than trusting the caller: the
    project's saved style is what actually went into the ASS, and a mismatch between
    the two is exactly the case where libass would silently substitute.
    """
    family = _font_family_from_ass(subtitles)
    if family is None:
        return
    ensure_font_installed(SubtitleStyle(font_name=family))


def _font_family_from_ass(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Style: Default,"):
            fields = line.split(",")
            return fields[1].strip() if len(fields) > 1 else None
    return None


def concat(
    ffmpeg: str,
    *,
    parts: list[Path],
    target: Path,
) -> VideoResult:
    """Join chunks with the concat demuxer and ``-c copy`` (§19.27).

    Stream copy, not a re-encode: every chunk was produced by the same encoder with the same
    settings, so joining them costs one read and one write per byte — measured 3.1 s for 1078 MB
    — and cannot change a pixel. Re-encoding here would throw away the only reason the chunks
    exist.

    The list is written **absolute** with forward slashes: the demuxer resolves relative paths
    against the *script's* directory, so a relative entry silently points at a file that is not
    there and fails as "视频渲染失败" naming the wrong thing (§19.23 trap).
    """
    if not parts:
        raise AppError("video_concat_empty", "没有可拼接的分段")
    for part in parts:
        if not part.is_file() or part.stat().st_size == 0:
            raise AppError(
                "video_concat_missing",
                f"分段文件不见了：{part.name}",
                hint="重新导出；如果反复失败，检查磁盘空间。",
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    script = target.with_suffix(target.suffix + ".concat.txt")
    script.write_text(
        "".join(f"file '{part.resolve().as_posix()}'\n" for part in parts),
        encoding="utf-8",
        newline="\n",
    )
    try:
        _run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(script),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
    finally:
        script.unlink(missing_ok=True)

    if not target.is_file() or target.stat().st_size == 0:
        raise AppError("video_concat_empty_file", "拼接产出为空文件", hint="检查 ffmpeg 日志。")
    # ``frames`` is unknown without decoding the whole file, and the caller does not need it:
    # the film's length comes from the probe. Same posture as :func:`mux`.
    return VideoResult(
        path=target, duration_ms=_probe_duration_ms(ffmpeg, target), frames=0
    )


def render_still(
    ffmpeg: str,
    *,
    target: Path,
    at_ms: int,
    width: int,
    height: int,
    background: Path,
    layers: list[Layer],
    box: box_track.BoxChain | None = None,
    box_segment: box_track.BoxSegment | None = None,
    box_source: Path | None = None,
    box_seek_ms: int = 0,
    box_keep_timestamps: bool = False,
    top: box_track.BoxChain | None = None,
    top_segment: box_track.BoxSegment | None = None,
    top_source: Path | None = None,
    top_seek_ms: int = 0,
    top_keep_timestamps: bool = False,
    avatars: list[AvatarLayer] | None = None,
    text: Layer | None = None,
    subtitles: Path | None = None,
) -> VideoResult:
    """Composite one frame at ``at_ms`` for the preview (§19.7).

    A still is ~200 ms end to end, which is what makes v1's preview honest: the user
    drags the playhead and sees the real layout instead of a hand-drawn approximation.
    Real-time playback would need a second render pipeline and a second clock.

    ``-ss`` is deliberately **not** used, for the reason given on
    :func:`build_filtergraph`: seeking resets the frame's PTS to 0, and ``ass`` would
    then draw nothing because no cue covers 0. The offset is applied as a PTS shift on
    an infinitely looping still instead, which costs a few frames of decode and is the
    only version that actually shows the cue.

    ``box_segment`` is the one box content stretch covering ``at_ms`` — a block's image or
    clip, or a plate excerpt over a gap. Only that one is declared as an input, so a still
    costs the same whether the film has two blocks or two hundred.

    ``box_seek_ms``/``top_seek_ms`` place the frame inside a **clip**, and they are input-side
    ``-ss`` rather than the PTS shift the graph uses for images. The shift is exact for a looped
    still and wrong for a clip: ``setpts=PTS-42.32/TB`` followed by ``fps=24`` emits the clip's
    first frame, because the frames the shift pushed to a negative timestamp are simply the ones
    ``fps`` starts from. Measured: the still inside a 292 s clip at a 42.32 s offset was
    byte-identical to the source at **0.0 s**, and the ``-ss`` render differed from the true frame
    in 0.00 % of pixels. That bug predates P1 and made every still inside a video block a lie.

    ``box_keep_timestamps``/``top_keep_timestamps`` then fix the frame *within* that clip. Plain
    ``-ss t`` is **ceil** — the first frame at or after t — while the film's ``fps=24`` grid is
    **floor**, the frame whose interval contains t. When t falls inside a frame the two rules
    disagree, and the preview shows the next frame while the film shows this one; measured at
    500 670 ms the two rules were 17.62 % of the box apart. With ``keep_timestamps`` the input is
    seeked a second early under ``-copyts`` and the graph's ``trim`` picks the frame, which is
    byte-identical to the film's.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = build_filtergraph(
        width=width,
        height=height,
        layers=layers,
        subtitle_path=subtitles,
        offset_ms=max(0, at_ms),
        box=box,
        top=top,
        avatars=avatars,
        text=text,
    )
    script = target.with_suffix(target.suffix + ".filtergraph")
    script.write_text(graph, encoding="utf-8")

    command = [ffmpeg, "-nostdin", "-y", "-loglevel", "error"]
    if subtitles is not None:
        ensure_font_ok(subtitles)
    for path, layer in [(background, None), *((layer.path, layer) for layer in layers)]:
        if layer is not None and layer.is_video:
            command += ["-i", str(path)]
        else:
            command += ["-loop", "1", "-i", str(path)]
    if box_segment is not None and box_source is not None:
        command += box_track.input_args(
            box_segment,
            box_source,
            seek_ms=box_seek_ms,
            keep_timestamps=box_keep_timestamps,
        )
    for avatar in avatars or []:
        if avatar.is_video:
            command += ["-i", str(avatar.path)]
        else:
            command += ["-loop", "1", "-i", str(avatar.path)]
    if text is not None:
        command += ["-loop", "1", "-i", str(text.path)]
    # A still needs at most one segment per chain, and neither is ever a placeholder: the
    # frame is drawn by exactly one of the two, and the covering segment is the one that has
    # something to show (§19.26).
    if top_segment is not None and top_source is not None:
        command += box_track.input_args(
            top_segment,
            top_source,
            seek_ms=top_seek_ms,
            keep_timestamps=top_keep_timestamps,
        )

    command += [
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-c:v",
        "png",
        # Level 1 rather than zlib's default 6: measured on a real 1920×1080 preview frame,
        # the encode drops 330 ms → 217 ms (**−113 ms, −34 %**) at PSNR `inf` — the pixels are
        # unchanged, only the deflate effort is. The file grows 1364 → 1596 KB (+17 %), which
        # is free: a preview PNG is a local temp artefact and is never delivered as a
        # master. Level 0 is faster still (181 ms) but writes 8.1 MB, and that would be paid
        # back over the HTTP fetch the browser makes for it.
        "-compression_level",
        "1",
        str(target),
    ]

    _run(command, timeout=120)
    script.unlink(missing_ok=True)
    if not target.exists() or target.stat().st_size == 0:
        raise AppError("preview_failed", "静帧预览生成失败", hint="检查 ffmpeg 日志。")
    return VideoResult(path=target, duration_ms=0, frames=1)


def composite_still(
    ffmpeg: str,
    *,
    target: Path,
    width: int,
    height: int,
    segment: box_track.BoxSegment,
    source: Path,
    box_w: int,
    box_h: int,
    box_x: int,
    box_y: int,
) -> Path:
    """Render **one still material segment** into a single PNG (§19.28).

    The point is that a static picture is static: the chain used to redraw it 24 times a second
    — ``boxblur`` radius 20 power 2 on 1440x810, then scale/overlay/format/pad onto the canvas.
    Measured on chunk 2 of 费那奇MV, the filtergraph was 88 % of the encode and x264 only 12 %;
    looping one PNG instead dropped that chunk from 69-93 s / 3.6-4.7 GB to 18-21 s / 2.22 GB.

    ``width``/``height`` are the chain's own target size — the canvas for the split (RGBA) chain,
    the material box for the opaque one — and ``box_w``/``box_h``/``box_x``/``box_y`` describe
    where the box sits inside it. The normalisation is :func:`box_track.canvas_piece`, the very
    function the chain calls, rather than a second spelling of "fit this file into the box": the
    arithmetic that is easy to get wrong is the *input number* inside that fragment, and a
    duplicate implementation is how the preview and the export start to disagree.

    The PNG keeps its alpha channel. Outside the box the chain's pad is ``black@0`` so the
    template's own plate and artwork still show through, and the composite has to preserve that
    or the box would sit on a black rectangle.
    """
    piece = box_track.canvas_piece(
        segment,
        canvas_w=width,
        canvas_h=height,
        box_w=box_w,
        box_h=box_h,
        box_x=box_x,
        box_y=box_y,
        # This command declares exactly one input, so the segment's film-wide input number has
        # to be shifted all the way to 0. Anything else reads a stream that does not exist.
        input_base=-segment.input_index,
        tag=0,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-filter_complex",
            f"{piece}[out]",
            "-map",
            "[out]",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-c:v",
            "png",
            str(target),
        ],
        timeout=300,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise AppError("preview_failed", "静帧合成失败", hint="检查 ffmpeg 日志。")
    return target


@dataclass(frozen=True)
class AudioMix:
    """One block's own sound, placed on the output timeline.

    ``start_ms``/``end_ms`` are **output** milliseconds (§19.3): the block may be longer on
    the original timeline than what the film plays of it. The clip is always read from its own
    beginning, because that is what its picture does.

    ``solo`` is 「S」 (§19.24): the podcast is cut out over this stretch and the clip plays
    alone, instead of the two being summed. One flag rather than a second list, because a
    stretch cannot sensibly be both mixed and replacing.
    """

    start_ms: int
    end_ms: int
    path: Path
    solo: bool = False


def mux(
    ffmpeg: str,
    *,
    video: Path,
    audio: Path,
    target: Path,
    mix: list[AudioMix] | None = None,
) -> VideoResult:
    """Stick the rendered audio onto the silent video, mixing in blocks' own sound if any.

    ``-c copy`` measured 0.24 s per 60 s — three orders of magnitude cheaper than
    encoding audio here, and it guarantees the film's sound is bit-identical to the
    audio the user already exported and approved. That path is kept for the **silent**
    case, which is the default and every project until someone unmutes a block (§19.19).

    ``mix`` is the audible stretches, each labelled ``solo`` or not. Either way it cannot be a
    stream copy — two audio tracks have to be resampled, delayed and summed — so that case
    re-encodes the podcast audio once, at a bitrate high enough that the difference is
    inaudible. It is opt-in per block, so nobody pays it by accident.

    ``-shortest`` is deliberately **not** used: the two tracks are both derived from
    the same EDL, and if they ever disagree slightly, silently truncating one hides a
    real bug behind a plausible-looking file.
    """
    if not video.is_file():
        raise AppError("video_missing", "视频轨还没渲染好", status=404)
    if not audio.is_file():
        raise AppError(
            "audio_missing",
            "成片音频还没渲染好",
            hint="先导出成片音频，再导出视频。",
            status=404,
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    audible = [item for item in (mix or []) if item.end_ms > item.start_ms]
    if not audible:
        _run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
    else:
        graph, args = _mix_graph(audible)
        _run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-i",
                str(audio),
                *args,
                "-filter_complex",
                graph,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                MIX_BITRATE,
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
    if not target.exists() or target.stat().st_size == 0:
        raise AppError("mux_failed", "混流产出为空文件", hint="检查 ffmpeg 日志与磁盘空间。")

    duration = _probe_duration_ms(ffmpeg, target)
    return VideoResult(path=target, duration_ms=duration, frames=0)


#: Podcast bitrate for the mixed case. The audio render defaults to 192k; matching it
#: means the only difference the user can hear is the sound they asked for.
MIX_BITRATE: Final[str] = "192k"

#: Everything is resampled to this before summing. ``amix`` refuses inputs whose formats
#: disagree, and a vp9/opus screen recording from a phone is 48 kHz stereo while an older
#: clip is 44.1 kHz mono — the failure is an ffmpeg error at encode time, i.e. after the
#: twenty-minute video encode, which is the worst possible moment to learn about it.
MIX_SAMPLE_RATE: Final[int] = 48000


def _mix_graph(stretches: list[AudioMix]) -> tuple[str, list[str]]:
    """``(filter_complex, extra input arguments)`` for podcast + block sound.

    One input per stretch rather than per file: the same clip can back two blocks at
    different instants, and a `-ss`-style share would put one of them at the wrong time.
    ``amix`` with ``duration=first`` keeps the film's length: the podcast track is input 1
    and is the full film, so a stretch reaching past the end cannot lengthen the output.

    ``normalize=0`` is not decoration. ``amix``'s default is to divide by the number of
    inputs, which would quietly halve the podcast's volume the moment one block is unmuted —
    a change nobody asked for, in the direction of "the film sounds worse but only sometimes".

    With 「S」 in play the podcast is silent wherever the clip speaks, so the two never sum to
    more than either one — which is why the limiter below is left alone rather than retuned. A
    solo stretch that happens to overlap a mixed one is the caller's business, not this
    function's: the plan refuses to produce both for the same block (§19.24.1).

    ``alimiter`` is the other half of that decision, and it was measured rather than assumed:
    with ``normalize=0`` and both inputs at full scale, **36 000 of 96 000 samples** landed on
    the 16-bit ceiling. ``:latency=1`` is not decoration either — the limiter delays its output
    by 239 samples (5 ms) by default, which would put every clip's sound 5 ms behind its
    picture. Measured with an impulse: 0 samples of delay with the flag.
    """
    # Every 「S」 stretch silences the podcast over its own span. Doing it with `volume=enable`
    # rather than by splitting and concatenating keeps the podcast one continuous stream: its
    # duration stays exactly the film's, so `amix:duration=first` still ends the film correctly.
    muted_spans = "".join(
        f",volume=enable='between(t,{stretch.start_ms / 1000:.3f},"
        f"{stretch.end_ms / 1000:.3f})':volume=0"
        for stretch in stretches
        if stretch.solo
    )
    parts: list[str] = [
        f"[1:a]aresample={MIX_SAMPLE_RATE},aformat=channel_layouts=stereo{muted_spans}[pod]"
    ]
    labels = ["[pod]"]
    args: list[str] = []
    for index, stretch in enumerate(stretches):
        args += ["-i", str(stretch.path)]
        # Input 0 is the video and input 1 the podcast, so this stretch is input index + 2.
        parts.append(
            f"[{index + 2}:a]aresample={MIX_SAMPLE_RATE},"
            f"aformat=channel_layouts=stereo,"
            f"atrim=start=0:end={(stretch.end_ms - stretch.start_ms) / 1000:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"adelay={stretch.start_ms}|{stretch.start_ms}[bx{index}]"
        )
        labels.append(f"[bx{index}]")
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:"
        f"dropout_transition=0:normalize=0,"
        # 0.95 rather than 1.0: a limiter set to exactly full scale still lets a sample or two
        # through at the ceiling once the encoder rounds.
        f"alimiter=limit=0.95:level=disabled:latency=1[aout]"
    )
    return ";".join(parts), args


def _probe_duration_ms(ffmpeg: str, path: Path) -> int:
    """Read the muxed file's duration, so the caller never has to guess it."""
    try:
        completed = subprocess.run(
            [ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    text = (completed.stdout or "").strip()
    try:
        return round(float(text) * 1000)
    except ValueError:
        return 0
