"""Bake the static plate into one PNG, and carry the text where avatars cover it (§19.30, E1).

The export graph used to composite four full-canvas layers on **every frame**: the compiled
poster, the frame stroke, and the editable text, each one an ``overlay`` with a ``scale`` in
front of it. None of them changes during the film, so all of that work was redone 24 times a
second for nothing. Measured on the sample film: 53.48 → 33.86 s per 100 s of film (−36.7 %)
and peak RSS 3.04 → 2.31 GB, with a frame-by-frame PSNR of **``inf``** against the old path.

Three things this module has to get right, each one learned the hard way:

**The text cannot simply be baked in.** The graph draws ``background → stroke → material
chain → avatars → text → subtitles`` (:func:`animatter.media.video.build_filtergraph`), so
text sits *above* the avatars: an avatar slot's rect contains its own name plate. Flattening
the text into the plate would put it *under* the avatar and the name would vanish — measured
41.74 dB, i.e. visibly crushed text. So the pixels of the text that fall inside a drawn
avatar's rect are composited into **that avatar's own PNG** instead, which restores the
original order exactly.

**The avatar must be scaled by ffmpeg before the patch is drawn.** The graph scales the
avatar PNG (236×249 on the sample) to the slot rect (177×188) with swscale. Compositing the
text patch onto the *unscaled* PNG and letting the graph scale afterwards re-samples the text
along with the photo: measured 54.38 dB, 8 178 pixels off by more than 4, all of them along
the avatar's edges. Scaling with ffmpeg's own ``scale`` first and compositing at native
resolution instead makes the graph's later ``scale`` an identity — verified on the real
PNGs: re-scaling an already-scaled image at the same size is byte-identical for every
``flags=`` value. That version measured **PSNR ``inf``**.

**Both preconditions are checked, not assumed.** The bake is only exact if (a) no text pixel
falls inside the material box (the box is drawn above the plate, so baked text there would be
hidden), and (b) every avatar pixel under a text pixel is fully opaque (a half-transparent
avatar pixel would blend with the plate underneath it in the old path and with the baked text
in the new one). If either fails the caller falls back to baking the poster and stroke only
(−16.8 %, still ``inf``) and keeps the text as a layer.

Artefacts are named after a digest of everything that changes their pixels, so editing the
title, re-binding an avatar or re-importing the PSD produces a different file name rather
than a stale picture under an unchanged one (§19.11 trap 24).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import numpy as np
from PIL import Image

from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

#: Bumped when a change makes an existing ``plate-*.png`` show something the code no longer
#: produces. Part of the digest, so an old plate stops being reused rather than going stale.
#:
#: 2 (round 137): the plate and the stroke are separate files at their own rects instead of
#: full-canvas images assumed to sit at 0,0. Same pixels, different inputs.
PLATE_VERSION: Final[int] = 2

#: A pixel is "text" when its alpha is at least this. **1, not 8**: the composite has to
#: reproduce every pixel the old graph's ``overlay`` touched, and an alpha of 1/255 still
#: moves an opaque base by one unit. A threshold above 1 would leave a few hundred faint
#: edge pixels unpatched, and the acceptance bar is `inf`, not "invisible".
TEXT_ALPHA_MIN: Final[int] = 1


@dataclass(frozen=True)
class Art:
    """One artwork PNG and where it goes on the canvas.

    ``width``/``height`` default to the canvas, which is right for a full-frame layer. The
    media-box plate and the cropped stroke are **not** full-frame: scaling a 1444×815 plate to
    1920×1080 would smear it across the poster and off its own edge, so their own size travels
    with them.
    """

    path: Path
    x: int = 0
    y: int = 0
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class PatchSlot:
    """One avatar slot the export will actually draw, in canvas coordinates.

    ``lit``/``dim`` are the PNGs the graph would have used; ``w``/``h`` is where the graph
    scales them to, which is also the size the patch has to be drawn at.
    """

    name: str
    x: int
    y: int
    w: int
    h: int
    lit: Path
    dim: Path


@dataclass(frozen=True)
class Plate:
    """The baked plate, plus what the graph must do differently because of it.

    ``text`` is the text layer the caller still has to overlay, or ``None`` when it went into
    the plate. ``avatar_sources`` overrides a track's lit/dim PNGs for the slots that carry
    text pixels; a slot that is absent keeps its own files (its patch would be a no-op).
    """

    path: Path
    text: Path | None
    avatar_sources: dict[str, tuple[Path, Path]] = field(default_factory=dict)
    text_baked: bool = False
    patched_pixels: int = 0
    #: Why the text was left out of the plate, for the log line. Empty when it went in.
    skipped: str = ""


def _ident(path: Path, what: str) -> str:
    """A digest-safe identity for one input file.

    Name, size and mtime rather than a hash of the bytes: these PNGs are tens of KB to a few
    MB and re-hashing them per export would cost more than the bake it guards. The name
    already carries the template build and the text-edit signature.
    """
    try:
        info = path.stat()
    except OSError as exc:
        raise AppError(
            "plate_source_missing",
            f"烘焙底板需要的文件不在了：{path.name}",
            hint="重新导入这份 PSD 让模板重新编译。",
            status=409,
        ) from exc
    return f"{what}:{path.name}:{info.st_size}:{info.st_mtime_ns}"


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _text_mask(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA").getchannel("A")) >= TEXT_ALPHA_MIN


def _alpha(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA").getchannel("A"))


def _rect_mask(shape: tuple[int, int], rect: tuple[int, int, int, int]) -> np.ndarray:
    """A boolean mask of one canvas rectangle, clipped to the canvas."""
    x, y, w, h = rect
    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    x0, x1 = max(0, x), min(width, x + w)
    y0, y1 = max(0, y), min(height, y + h)
    if x0 < x1 and y0 < y1:
        mask[y0:y1, x0:x1] = True
    return mask


def _composite(
    ffmpeg: str, *, target: Path, layers: list[Art], width: int, height: int
) -> Path:
    """Composite ``layers`` bottom-up into one PNG at ``width`` × ``height``.

    Each input gets the same ``scale``+``setsar=1`` the graph applies to its own branch: the
    compiled PNGs carry no pixel aspect, and a SAR mismatch makes ``overlay`` scale a layer
    silently. ``format=auto`` is what :func:`~animatter.media.video.build_filtergraph` uses,
    so this is the same arithmetic in the same precision rather than a second spelling of it.

    ``layers[0]`` is the canvas-sized base; every later layer is drawn at its own size and
    position, which is what lets the media-box plate and the cropped stroke go where they
    belong instead of being stretched over the whole poster.

    The output **keeps its alpha channel**. Flattening to RGB looks harmless — the compiled
    poster is 99.94 % opaque — but it moves a thousand pixels from alpha 254 to 255, and
    every later overlay in the graph blends against that alpha. Keeping RGBA makes "poster
    over stroke, then the rest" identical to the old chain instead of merely close.
    """
    if not layers:
        raise AppError("plate_empty", "底板烘焙没有输入")
    parts: list[str] = []
    current = ""
    for index, layer in enumerate(layers):
        layer_w = layer.width if layer.width is not None else width
        layer_h = layer.height if layer.height is not None else height
        parts.append(f"[{index}:v]scale={layer_w}:{layer_h},setsar=1[s{index}]")
        if index == 0:
            current = f"s{index}"
        else:
            parts.append(
                f"[{current}][s{index}]overlay=x={layer.x}:y={layer.y}:format=auto[m{index}]"
            )
            current = f"m{index}"

    command = [ffmpeg, "-nostdin", "-y", "-loglevel", "error"]
    for layer in layers:
        command += ["-i", str(layer.path)]
    command += [
        "-filter_complex",
        ";\n".join(parts),
        "-map",
        f"[{current}]",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-c:v",
        "png",
        "-pix_fmt",
        "rgba",
        # One 1080p still, decoded once per chunk: the file size does not matter next to the
        # encode, and the default search would spend a second per export on it. Same setting
        # the still preview settled on (§19.33 P0, 330 → 217 ms at PSNR inf).
        "-compression_level",
        "1",
        str(target),
    ]
    from animatter.media.video import _run as run_ffmpeg  # shared error handling

    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(command, timeout=300)
    if not target.is_file() or target.stat().st_size == 0:
        raise AppError("plate_bake_failed", "底板烘焙失败", hint="检查 ffmpeg 日志与磁盘空间。")
    return target


def _scale_avatar(ffmpeg: str, *, source: Path, target: Path, width: int, height: int) -> Path:
    """Downscale one avatar PNG **with ffmpeg**, not PIL (§19.30).

    ``flags=bicubic`` is spelled out even though it is swscale's default at this ratio: the
    point is that this is the *same* filter the graph applies, so the graph's later ``scale``
    becomes an identity. Verified on the sample's 236×249 PNGs — a second scale at the same
    size changes **0** pixels for every ``flags=`` value — so it is running the scale through
    ffmpeg that makes the patch exact, not the flag choice.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        f"scale={width}:{height}:flags=bicubic,setsar=1",
        "-frames:v",
        "1",
        "-c:v",
        "png",
        "-pix_fmt",
        "rgba",
        "-compression_level",
        "1",
        str(target),
    ]
    from animatter.media.video import _run as run_ffmpeg

    run_ffmpeg(command, timeout=120)
    return target


def _slug(name: str) -> str:
    """A file-name-safe slot name — the same rule ``services.video_render._slug`` uses."""
    cleaned = "".join(char if char.isalnum() else "-" for char in name).strip("-")
    return cleaned[:40] or "avatar"


def _patch_avatars(
    ffmpeg: str, *, text: Path, mask: np.ndarray, slots: list[PatchSlot], work_dir: Path
) -> tuple[dict[str, tuple[Path, Path]], int, str]:
    """Composite the text pixels inside each drawn avatar onto that avatar's PNGs.

    Returns the overrides, how many text pixels were moved, and an **empty** string when the
    patch is exact. An empty override map with an empty reason means "no text under any
    avatar", which is also exact — the text can still be baked.

    A non-empty reason means the patch would not be exact, and the caller keeps the text as a
    layer. Every slot is validated before any of them is returned: a plate with three patched
    avatars and one unpatched one is not a smaller optimisation, it is a film with a name
    missing.
    """
    out: dict[str, tuple[Path, Path]] = {}
    total = 0
    with Image.open(text) as image:
        source = image.convert("RGBA")
    try:
        for slot in slots:
            if slot.w <= 0 or slot.h <= 0:
                return {}, 0, f"slot_geometry_{slot.name}"
            inside = _rect_mask(mask.shape, (slot.x, slot.y, slot.w, slot.h)) & mask
            if not inside.any():
                # This slot's own name plate is not inside it, so the avatar covers no text.
                continue
            crop = source.crop((slot.x, slot.y, slot.x + slot.w, slot.y + slot.h))
            paths: list[Path] = []
            for state, original in (("lit", slot.lit), ("dim", slot.dim)):
                base = _digest(
                    [_ident(original, state), _ident(text, "text"), f"{slot.w}x{slot.h}", f"v{PLATE_VERSION}"]
                )
                scaled = work_dir / f"avpatch-{_slug(slot.name)}-{state}-{base}.png"
                if not scaled.is_file():
                    _scale_avatar(
                        ffmpeg, source=original, target=scaled, width=slot.w, height=slot.h
                    )
                # Checked **after** the scale, because the graph blends the text over this
                # exact PNG: a half-transparent pixel here is a pixel whose colour would move.
                alpha = _alpha(scaled)
                if alpha.shape != (slot.h, slot.w):
                    return {}, 0, f"slot_size_{slot.name}"
                ys, xs = np.nonzero(inside)
                under = alpha[ys - slot.y, xs - slot.x]
                if int(under.min()) != 255:
                    return {}, 0, f"slot_translucent_{slot.name}"
                patched = scaled.with_name(f"avpatch-{_slug(slot.name)}-{state}x-{base}.png")
                if not patched.is_file():
                    with Image.open(scaled) as base_image:
                        canvas = base_image.convert("RGBA").copy()
                    # Source-over at 1:1 — what the graph's `overlay` does when the base is
                    # opaque there, which is exactly what the check above just proved.
                    canvas.alpha_composite(crop)
                    canvas.save(patched)
                paths.append(patched)
            out[slot.name] = (paths[0], paths[1])
            total += int(inside.sum())
    finally:
        source.close()

    if out:
        # A patch pair is 100–200 KB and one is written per export; keeping every digest would
        # grow the output dir with each title edit.
        keep = {path for pair in out.values() for path in pair}
        for stale in work_dir.glob("avpatch-*.png"):
            if stale not in keep:
                stale.unlink(missing_ok=True)
    return out, total, ""


def bake(
    ffmpeg: str,
    *,
    base: Path,
    plate: Art | None,
    stroke: Art | None,
    text: Path | None,
    canvas_w: int,
    canvas_h: int,
    box_rect: tuple[int, int, int, int] | None,
    slots: list[PatchSlot],
    work_dir: Path,
) -> Plate:
    """Bake poster + plate + stroke (+ text) into one PNG, patching avatars that cover text.

    ``base`` is the poster with the media-box plate **removed**; ``plate`` and ``stroke`` are
    drawn at their own rects. Baking them back in is exact (each was extracted by proving that
    re-compositing it reproduces the original poster byte for byte), so the plate can be
    positioned from ``slots_json`` instead of being frozen into the poster.

    A template compiled before the split has no ``plate`` and keeps its old full-canvas stroke:
    then this is the same composite it always was.

    Always returns a :class:`Plate` when the poster itself is bakeable: a precondition that
    fails only drops the **text** from the plate (the measured −16.8 % fallback), it never
    blocks a film on an optimisation.
    """
    overlays: list[Art] = [Art(base)]
    if plate is not None and plate.path.is_file():
        overlays.append(plate)
    if stroke is not None and stroke.path.is_file():
        overlays.append(stroke)

    text_baked = False
    #: Whether the text pixels actually went into the composite. ``text_baked`` is also set
    #: when the overlay is entirely transparent — then the layer is *dropped*, not merged.
    text_merged = False
    skipped = "no_text"
    patches: dict[str, tuple[Path, Path]] = {}
    patched_pixels = 0
    if text is not None and text.is_file():
        mask = _text_mask(text)
        if not mask.any():
            # A fully transparent overlay: the graph would pay an input, a scale and an
            # overlay per frame to draw nothing, so drop it instead of compositing it.
            text_baked, skipped = True, ""
        elif box_rect is not None and (_rect_mask(mask.shape, box_rect) & mask).any():
            # The material box is drawn on top of the plate: baked text there would vanish.
            skipped = "text_in_box"
        else:
            patches, patched_pixels, reason = _patch_avatars(
                ffmpeg, text=text, mask=mask, slots=slots, work_dir=work_dir
            )
            if reason:
                skipped = reason
            else:
                text_baked, text_merged, skipped = True, True, ""

    stroke_part = (
        f"{_ident(stroke.path, 'stroke')}@{stroke.x},{stroke.y},{stroke.width}x{stroke.height}"
        if stroke is not None and stroke.path.is_file()
        else "-"
    )
    plate_part = (
        f"{_ident(plate.path, 'plate')}@{plate.x},{plate.y},{plate.width}x{plate.height}"
        if plate is not None and plate.path.is_file()
        else "-"
    )
    if text_baked and text is not None:
        text_part = _ident(text, "text")
        slot_part = ",".join(
            f"{_ident(slot.lit, 'lit')}/{_ident(slot.dim, 'dim')}"
            for slot in sorted(slots, key=lambda item: item.name)
        )
    else:
        text_part = "-"
        slot_part = "-"
    digest = _digest(
        [
            f"v{PLATE_VERSION}",
            f"{canvas_w}x{canvas_h}",
            _ident(base, "base"),
            plate_part,
            stroke_part,
            text_part,
            slot_part,
        ]
    )
    target = work_dir / f"plate-{digest}.png"
    if text_merged and text is not None:
        overlays.append(Art(text))
    _composite(ffmpeg, target=target, layers=overlays, width=canvas_w, height=canvas_h)

    for stale in work_dir.glob("plate-*.png"):
        if stale != target:
            stale.unlink(missing_ok=True)

    log.info(
        "plate.baked",
        target=target.name,
        layers=len(overlays),
        text_baked=text_baked,
        patched_pixels=patched_pixels,
        skipped=skipped or "-",
    )
    return Plate(
        path=target,
        text=None if text_baked else text,
        avatar_sources=patches,
        text_baked=text_baked,
        patched_pixels=patched_pixels,
        skipped=skipped,
    )
