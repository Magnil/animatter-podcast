"""The box content chain: many segments in, **one** overlay out (§19.5).

Why a chain rather than one overlay per block
--------------------------------------------
The obvious build is one ``overlay`` per timeline block, each with
``enable='between(t,…)'``. Measured on the avatar version of the same problem, that dies:
1200 intervals produced a 79 KB expression and ffmpeg refused to start with
``The filename or extension is too long`` — a ``CreateProcess`` limit that reads like a
path bug.

The second obvious build — render the box content into its own file first, then overlay
that file — costs a whole extra x264 pass. Encoding is ~11 s per 60 s of output on this
machine, so a second pass would add ~8 minutes to a one-hour export for nothing.

So the segments are concatenated **inside the same filtergraph** and the result is
overlaid once. The graph grows with the number of *segments*, not with the length of the
film, and there is exactly one extra overlay no matter how many blocks the user has.

Input numbering
---------------
The chain does not own the ffmpeg input list; the caller does, because the film already
spends input 0 on the compiled background. Every function here takes ``first_input`` and
labels its references from there, so the two never disagree about which ``[N:v]`` is
which — a mismatch fails loudly in ffmpeg but the error names a filter, not a number.

The gap problem
---------------
Blocks need not cover the whole timeline, but an ``overlay`` input must exist for the
entire film or the overlay stops early and the frame freezes. Gaps are therefore filled
with an excerpt **of the plate itself**, cropped out of the compiled background. The
overlay is then always opaque and always correct: over a gap it draws the plate
pixel-for-pixel over the plate, which is what "no content" is supposed to look like.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError

#: Matches the film's frame rate. A mismatch makes ``concat`` duplicate or drop frames at
#: every boundary, which stutters exactly where a picture changes — the one moment the
#: viewer is looking.
FPS: Final[int] = 25

#: Blurred backdrop for ``blur_fill``. Radius 20 at 1440×810 makes text in the backdrop
#: unreadable (the point of the effect) without turning it to mush; ``boxblur`` is
#: separable so it stays cheap.
_BLUR_RADIUS: Final[int] = 20
_BLUR_POWER: Final[int] = 2
#: The backdrop is darkened: a bright poster blurred behind a bright poster leaves the
#: foreground with no edge to sit against.
_BACKDROP_DIM: Final[str] = "eq=brightness=-0.10"

KIND_IMAGE = "image"
KIND_VIDEO = "video"
#: A stretch of timeline with no block: show the plate, so the overlay stays opaque.
KIND_PLATE = "plate"
#: A placeholder in the other of the two chains (§19.27). The lower chain draws box blocks and
#: the top chain draws 全屏 blocks; each still has to span the whole film, so wherever a chain
#: must draw nothing it draws this. It is a `color` **source filter**, not a file: no `-i`, no
#: entry in `sources`, no input number to get wrong.
KIND_TRANSPARENT = "transparent"


@dataclass(frozen=True)
class BoxSegment:
    """One stretch of box content.

    ``duration_ms`` is in **output** time — the caller already resolved it through the
    EDL, so nothing here needs to know that an edit happened.

    ``input_index`` is the ffmpeg input this segment reads. Two segments can share one
    (a gallery image that appears twice, a plate excerpt used for two gaps).

    ``crop`` is ``(x, y, w, h)`` in the source's own pixels; used by plate excerpts only.
    ``loop`` makes a short clip repeat to fill its stretch instead of freezing on its last
    frame — the difference between "a 3 s clip in a 10 s block" looking intentional and
    looking broken.
    """

    input_index: int
    duration_ms: int
    kind: str
    fit: str = "cover"
    crop: tuple[int, int, int, int] | None = None
    loop: bool = False
    #: ``box`` (default) keeps the picture inside the material box; ``full`` fills the whole
    #: canvas instead (§19.26). Per segment because one film can mix the two.
    placement: str = "box"


@dataclass(frozen=True)
class BoxChain:
    """A built chain plus what the compositor needs to place it."""

    text: str
    x: int
    y: int
    width: int
    height: int
    #: Distinct sources, in input order; ``input_index`` above indexes into this.
    sources: list[Path]
    #: The input number the chain's ``input_index == 0`` refers to. Set by build_chain.
    input_base: int = 0
    #: How many ffmpeg inputs the chain consumes, counted from the segments themselves.
    #:
    #: It has to be stored: the caller owns the ``-i`` list and ``sources`` is empty here
    #: (the service layer holds the paths), so nothing else knows the number. Before P3
    #: nothing downstream depended on it — the box was always overlaid last — but an avatar
    #: overlay comes *after* the box, and getting the count wrong asks ffmpeg for the wrong
    #: stream, which fails with "Error binding filtergraph inputs/outputs".
    inputs: int = 1

    @property
    def input_count(self) -> int:
        return max(self.inputs, len(self.sources))

    def text_for(self, *, input_base: int) -> str:
        """The chain, with every ``[N:v]`` shifted to start at ``input_base``.

        The chain is built once and placed once, but the *caller* owns the ffmpeg input
        list (the background is input 0 and the artwork layers follow). Re-basing here
        instead of at build time means the chain does not have to be rebuilt when the
        number of artwork layers changes.
        """
        if input_base == self.input_base:
            return self.text
        shift = input_base - self.input_base
        return re.sub(
            r"\[(\d+):v\]",
            lambda match: f"[{int(match.group(1)) + shift}:v]",
            self.text,
        )


def input_args(segment: BoxSegment, source: Path) -> list[str]:
    """The ffmpeg input declaration for one segment.

    A still needs ``-loop 1`` *and* ``-t``: without the explicit duration ffmpeg loops
    forever and the encode never ends. A clip needs neither, but a looping one needs
    ``-stream_loop -1`` so it repeats rather than running out.
    """
    if not source.is_file():
        raise AppError(
            "asset_missing",
            f"素材文件不见了：{source.name}",
            hint="在素材库里重新导入这张图或这段视频。",
            status=404,
        )
    seconds = f"{segment.duration_ms / 1000:.3f}"
    if segment.kind == KIND_VIDEO:
        prefix = ["-stream_loop", "-1"] if segment.loop else []
        return [*prefix, "-t", seconds, "-i", str(source)]
    return ["-loop", "1", "-t", seconds, "-i", str(source)]


def _blurred_backdrop(
    src_label: str, out_label: str, width: int, height: int
) -> str:
    """The blurred, dimmed ``cover`` of one image, filling the whole box, as ``out_label``.

    Shared by ``blur_fill`` and ``blur_crop`` on purpose: those two strategies differ only in
    what they put *on top* of the backdrop, so two copies of this chain would drift into looking
    like two different features — a different radius, a different dim, a different crop anchor.

    The caller names the output: each strategy has its own downstream reference to it, and a
    label the caller does not expect is a filtergraph that fails to bind at encode time.
    """
    return (
        f"[{src_label}]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:0:0,boxblur=luma_radius={_BLUR_RADIUS}:luma_power={_BLUR_POWER},"
        f"{_BACKDROP_DIM},setsar=1[{out_label}]"
    )


def _normalise(
    segment: BoxSegment, width: int, height: int, *, input_base: int = 0, tag: int = 0
) -> str:
    """Make one input fill ``width×height`` according to its kind and fit.

    Every branch ends with ``setsar=1``: the compiled PNGs and the user's own assets carry
    no pixel-aspect metadata, and a mismatch makes ``overlay`` silently rescale, landing
    the content a few pixels off from the box the user confirmed on screen.

    ``input_base`` shifts every ``[N:v]`` reference: the film already spends input 0 on the
    compiled background and one input per artwork layer, so the chain's own inputs start
    later. Offsetting here rather than in the caller keeps the arithmetic in one place.

    ``tag`` names the intermediate labels. It must be the segment's position in the chain,
    not its input number: the same file can legitimately appear twice in a chain (a gallery
    image shown again), and two filters cannot share a label — ffmpeg then reports "label
    already used" and names a variable, not a cause.
    """
    label = tag

    if segment.kind == KIND_TRANSPARENT:
        # A filter-only source, so this branch never touches `index`. Alpha is forced to 0
        # *after* the format conversion rather than written as `c=black@0.0`: the colour
        # filter's own alpha round-trip depends on its output format, while
        # `colorchannelmixer=aa=0` means the same thing in every ffmpeg build.
        seconds = f"{segment.duration_ms / 1000:.3f}"
        return (
            f"color=c=black:s={width}x{height}:r={FPS}:d={seconds},"
            f"format=rgba,colorchannelmixer=aa=0,setsar=1"
        )

    index = segment.input_index + input_base
    src = f"[{index}:v]"

    if segment.crop is not None:
        x, y, w, h = segment.crop
        return f"{src}crop={w}:{h}:{x}:{y},scale={width}:{height},setsar=1"

    fit = segment.fit
    if fit == "stretch":
        return f"{src}scale={width}:{height},setsar=1"
    if fit == "cover":
        # Top-left anchored, not centred: ``crop`` defaults to the middle, which throws away
        # the top *and* the bottom. The material is screenshots and posters — subject at the
        # top, dead margin at the bottom — so keeping the top edge is the only crop that
        # keeps the thing the picture is about.
        return (
            f"{src}scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:0:0,setsar=1"
        )
    if fit == "blur_fill":
        # Two branches from one input. ``split`` is required: feeding one input to two
        # filters without it makes ffmpeg reuse an already-consumed frame.
        return (
            f"{src}split=2[bgbg{label}][bgfg{label}];"
            f"{_blurred_backdrop(f'bgbg{label}', f'bgbase{label}', width, height)};"
            f"[bgfg{label}]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1"
            f"[bgfg2{label}];"
            f"[bgbase{label}][bgfg2{label}]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1"
        )
    if fit == "blur_crop":
        # **No scaling.** The picture keeps its own pixels — this is the one strategy for
        # material that must stay sharp, so blowing a 1080-wide screenshot up to 1440 would
        # defeat it.
        #
        # The anchor is the midpoint of the image's top edge (`overlay=(W-w)/2:0`): a wide
        # picture loses equal slices from both sides, a tall one loses only its bottom, and
        # whatever the box has left over is the blurred backdrop. `overlay` clips for us — the
        # parts outside the box are simply not drawn, which is the "超出部分裁掉" half.
        return (
            f"{src}split=2[bccg{label}][bcfg{label}];"
            f"{_blurred_backdrop(f'bccg{label}', f'bcbase{label}', width, height)};"
            # `setsar=1` on the foreground is what stops `overlay` from rescaling it: an
            # unknown SAR makes overlay re-fit the image, and the 1:1 placement is the point.
            f"[bcfg{label}]setsar=1[bcimg{label}];"
            f"[bcbase{label}][bcimg{label}]overlay=(W-w)/2:0:format=auto,setsar=1"
        )
    # `contain` and anything unrecognised: keep every pixel, letterbox the remainder.
    # Black rather than transparent because the result is overlaid onto the plate, and a
    # transparent pad would let the template's placeholder artwork show through the bars.
    return (
        f"{src}scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )


def build_chain(
    segments: list[BoxSegment],
    *,
    width: int,
    height: int,
    x: int = 0,
    y: int = 0,
    input_base: int = 0,
    out_label: str = "boxout",
) -> BoxChain:
    """Concatenate every segment into one ``width×height`` chain.

    ``trim`` is applied per segment even though the inputs are already bounded by ``-t``:
    a looping still emits at its own rate, and ``trim`` is what makes the *output* length
    exact rather than "close enough".

    ``input_index`` in each segment is an **ffmpeg input number**, so the caller's ``-i``
    list and this chain must agree. They do by construction: the caller asks
    :func:`build_chain` for the chain first and places those inputs at
    ``base + input_index``.
    """
    if not segments:
        raise AppError(
            "box_track_empty",
            "素材框里没有任何内容",
            hint="先往时间轴上放一个素材块。",
        )

    parts: list[str] = []
    labels: list[str] = []
    for position, segment in enumerate(segments):
        if segment.duration_ms <= 0:
            raise AppError(
                "box_segment_empty",
                "有一段素材的时长是 0",
                hint="把素材块的时长拉长，或者把它删掉。",
            )
        # ``position`` rather than ``input_index`` for the output label: the same input
        # can legitimately be concatenated twice, and two filters cannot share a label.
        parts.append(
            f"{_normalise(segment, width, height, input_base=input_base, tag=position)},"
            f"fps={FPS},trim=duration={segment.duration_ms / 1000:.3f},"
            f"setpts=PTS-STARTPTS[bxs{position}]"
        )
        labels.append(f"[bxs{position}]")

    if len(labels) == 1:
        parts.append(f"{labels[0]}null[{out_label}]")
    else:
        parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[{out_label}]")

    return BoxChain(
        text=";\n".join(parts),
        x=x,
        y=y,
        width=width,
        height=height,
        sources=[],
        input_base=input_base,
        # Placeholders are color filters, not -i inputs, so they must not be counted: this
        # number is what tells the compositor where the *next* layer's inputs begin.
        inputs=len(
            {
                segment.input_index
                for segment in segments
                if segment.kind != KIND_TRANSPARENT
            }
        ),
    )


def build_single(
    segment: BoxSegment,
    *,
    source_offset_ms: int,
    width: int,
    height: int,
    x: int = 0,
    y: int = 0,
    input_base: int = 0,
    out_label: str = "boxout",
) -> BoxChain:
    """One segment, seeked ``source_offset_ms`` into itself — for the still preview.

    Seeking the *chain* would mean building the whole film's chain for one frame. Picking
    the covering segment and shifting its PTS back is the same arithmetic and costs one
    input instead of hundreds. The shift is negative because ffmpeg starts the input at 0
    and the frame we want is that far in.
    """
    shift = f",setpts=PTS-{source_offset_ms / 1000:.3f}/TB" if source_offset_ms else ""
    remaining = max(1, segment.duration_ms - source_offset_ms)
    text = (
        f"{_normalise(segment, width, height, input_base=input_base, tag=0)}{shift},fps={FPS},"
        f"trim=duration={remaining / 1000:.3f},setpts=PTS-STARTPTS[{out_label}]"
    )
    return BoxChain(
        text=text,
        x=x,
        y=y,
        width=width,
        height=height,
        sources=[],
        input_base=input_base,
        inputs=1,
    )


def _canvas_piece(
    segment: BoxSegment,
    *,
    canvas_w: int,
    canvas_h: int,
    box_w: int,
    box_h: int,
    box_x: int,
    box_y: int,
    input_base: int,
    tag: int,
) -> str:
    """One segment normalised into a canvas-sized RGBA frame (§19.26).

    A ``full`` segment is normalised straight into the canvas; anything else is normalised
    into the material box and padded transparently out to the canvas at the box's position.
    The pad is ``black@0`` rather than black: outside the box the template's own background
    must show through, which is what the opaque chain achieved by redrawing the plate there.
    """
    if segment.placement == "full":
        return f"{_normalise(segment, canvas_w, canvas_h, input_base=input_base, tag=tag)},format=rgba"
    inner = _normalise(segment, box_w, box_h, input_base=input_base, tag=tag)
    return (
        f"{inner},format=rgba,"
        f"pad={canvas_w}:{canvas_h}:{box_x}:{box_y}:color=black@0,setsar=1"
    )


def build_canvas_chain(
    segments: list[BoxSegment],
    *,
    canvas_w: int,
    canvas_h: int,
    box_w: int,
    box_h: int,
    box_x: int = 0,
    box_y: int = 0,
    input_base: int = 0,
    out_label: str = "boxout",
) -> BoxChain:
    """Like :func:`build_chain`, but padded out to the whole canvas so 全屏 blocks fit.

    Only used when at least one block is ``placement='full'``. A separate function rather than
    a flag on :func:`build_chain` on purpose: this chain is RGBA and canvas-sized, and the
    default path must not change shape because an option exists.
    """
    if not segments:
        raise AppError(
            "box_track_empty",
            "素材框里没有任何内容",
            hint="先往时间轴上放一个素材块。",
        )

    parts: list[str] = []
    labels: list[str] = []
    for position, segment in enumerate(segments):
        if segment.duration_ms <= 0:
            raise AppError(
                "box_segment_empty",
                "有一段素材的时长是 0",
                hint="把素材块的时长拉长，或者把它删掉。",
            )
        piece = _canvas_piece(
            segment,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            box_w=box_w,
            box_h=box_h,
            box_x=box_x,
            box_y=box_y,
            input_base=input_base,
            tag=position,
        )
        parts.append(
            f"{piece},fps={FPS},trim=duration={segment.duration_ms / 1000:.3f},"
            f"setpts=PTS-STARTPTS[bxs{position}]"
        )
        labels.append(f"[bxs{position}]")

    if len(labels) == 1:
        parts.append(f"{labels[0]}null[{out_label}]")
    else:
        parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[{out_label}]")

    return BoxChain(
        text=";\n".join(parts),
        # The canvas, not the box: this chain covers the frame, so it is placed at the origin.
        x=0,
        y=0,
        width=canvas_w,
        height=canvas_h,
        sources=[],
        input_base=input_base,
        # Placeholders are color filters, not -i inputs, so they must not be counted: this
        # number is what tells the compositor where the *next* layer's inputs begin.
        inputs=len(
            {
                segment.input_index
                for segment in segments
                if segment.kind != KIND_TRANSPARENT
            }
        ),
    )
