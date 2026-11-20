"""A still inside a clip must show that clip's frame, not the clip's first frame (round 129).

**The bug.** `box_track.build_single` placed the requested instant by shifting PTS backwards
(``setpts=PTS-42.320/TB``) and then running ``fps=24``. The ``fps`` filter re-bases its output
timestamps, so the frames the shift pushed to a negative timestamp are simply the first ones it
emits — and every still inside a video block came out as the clip's **opening frame**.

Measured on the 费那奇MV sample before the fix (a 292 s clip, segment starting at 446 710 ms):

| instant   | offset into the clip | still vs source at 0 s | still vs source at 42.32 s |
|-----------|----------------------|------------------------|----------------------------|
| 489 030   | 42.32 s              | **0.00 %**             | 60.72 %                    |

So the still was byte-identical to frame zero of a clip it claimed to be 42 s into. The DOM stage
was right and the still was wrong — which is why the P1 consistency check could not be passed
until this was fixed. The fix seeks the **input** (``-ss``) and leaves the graph's timestamps
alone; verified against the ``-ss`` render at 42.32 s: **0.00 %** differing, while the old chain
differed from it in 60.75 %.

**Why this file rather than `test_video_render.py`.** These tests need a real four-colour clip:
the failure is a *pixel* failure, and the string checks in `test_video_render.py` cannot see it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from animatter.media import box_track
from animatter.media.box_track import KIND_IMAGE, BoxSegment

FFMPEG = shutil.which("ffmpeg")


# --- the argument shape, which needs no ffmpeg --------------------------


def test_a_seeked_clip_declares_ss_before_the_input(tmp_path: Path) -> None:
    """``-ss`` is an **input** option, and its position after ``-stream_loop`` is load-bearing.

    Measured: ``-stream_loop -1 -ss 300.281`` on a 292 s clip returns the frame at 8.0 s, which is
    what a looped segment 8 s past the clip's end means. Writing the ``-ss`` first loses the wrap.
    """
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    segment = BoxSegment(input_index=0, duration_ms=4000, kind=box_track.KIND_VIDEO, loop=True)
    args = box_track.input_args(segment, clip, seek_ms=2500)
    assert args.index("-stream_loop") < args.index("-ss")
    assert args[args.index("-ss") + 1] == "2.500"
    assert args.index("-ss") < args.index("-i")
    # The bound stays: without it a looped input never ends and the encode hangs.
    assert "-t" in args


def test_no_seek_means_no_ss(tmp_path: Path) -> None:
    """Zero offset must stay the cheap path: ``-ss 0.000`` on a clip costs a seek for nothing."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    page = tmp_path / "page.png"
    page.write_bytes(b"")
    video = box_track.input_args(
        BoxSegment(input_index=0, duration_ms=1000, kind=box_track.KIND_VIDEO), clip
    )
    assert "-ss" not in video
    still = box_track.input_args(
        BoxSegment(input_index=0, duration_ms=1000, kind=KIND_IMAGE), page
    )
    assert "-ss" not in still
    # An image is still looped with an explicit rate and bound.
    assert "-loop" in still and "-framerate" in still


def test_a_seeked_chain_does_not_shift_its_pts() -> None:
    """Both would move the picture by the offset **twice**.

    After an input-side seek the frame ffmpeg produces is already the right one, so the graph has
    to leave its timestamps alone — and must still trim to a single frame and re-base it, or
    ``-frames:v 1`` could pick a frame with a timestamp far from zero.
    """
    segment = BoxSegment(
        input_index=0, duration_ms=4000, kind=box_track.KIND_VIDEO, fit="contain"
    )
    seeked = box_track.build_single(
        segment, source_offset_ms=2500, width=160, height=90, input_seeked=True
    )
    assert "setpts=PTS-2.500/TB" not in seeked.text
    # The re-base after the trim stays: it is what puts the single frame at timestamp 0.
    assert "setpts=PTS-STARTPTS" in seeked.text
    assert "trim=end_frame=1" in seeked.text

    # And without the flag it still shifts, which is what a **still image** wants: one looped
    # frame has no second frame for `fps` to choose instead, so the shift is exact there.
    shifted = box_track.build_single(
        segment, source_offset_ms=2500, width=160, height=90
    )
    assert "setpts=PTS-2.500/TB" in shifted.text


# --- the pixels, which do -----------------------------------------------


@pytest.fixture(scope="module")
def colours_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A four-second clip: red, then green, then blue, then white — one second each."""
    if FFMPEG is None:
        pytest.skip("no ffmpeg on PATH")
    folder = tmp_path_factory.mktemp("colours")
    parts = []
    for colour in ("red", "green", "blue", "white"):
        part = folder / f"{colour}.mp4"
        subprocess.run(
            [
                FFMPEG, "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c={colour}:s=160x90:r=24:d=1",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                str(part),
            ],
            check=True,
            capture_output=True,
        )
        parts.append(part)
    listing = folder / "parts.txt"
    listing.write_text(
        "".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8"
    )
    clip = folder / "colours.mp4"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(clip),
        ],
        check=True,
        capture_output=True,
    )
    return clip


def _frame_pixel(clip: Path, offset_ms: int, *, tmp_path: Path) -> tuple[int, int, int]:
    """Render one still through the **same two functions** the preview uses."""
    segment = BoxSegment(
        input_index=0, duration_ms=4000, kind=box_track.KIND_VIDEO, fit="contain"
    )
    chain = box_track.build_single(
        segment,
        source_offset_ms=offset_ms,
        width=160,
        height=90,
        input_seeked=offset_ms > 0,
    )
    target = tmp_path / f"frame-{offset_ms}.png"
    completed = subprocess.run(
        [
            FFMPEG, "-nostdin", "-y", "-loglevel", "error",
            *box_track.input_args(segment, clip, seek_ms=offset_ms),
            "-filter_complex", chain.text_for(input_base=0),
            "-map", "[boxout]",
            "-frames:v", "1", "-update", "1", str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return Image.open(target).convert("RGB").getpixel((80, 45))


def test_a_still_shows_the_clips_frame_and_not_its_first_one(
    colours_clip: Path, tmp_path: Path
) -> None:
    """The regression in one assertion pair.

    Before the fix both instants returned red: the PTS shift plus ``fps`` always emitted the first
    frame, so the still inside a video block never moved no matter where the playhead was.
    """
    first = _frame_pixel(colours_clip, 0, tmp_path=tmp_path)
    third = _frame_pixel(colours_clip, 2_500, tmp_path=tmp_path)
    assert first[0] > 200 and first[1] < 80, f"0 ms should be red, got {first}"
    assert third[2] > 200 and third[0] < 80, f"2500 ms should be blue, got {third}"


def test_each_second_of_the_clip_is_a_different_frame(
    colours_clip: Path, tmp_path: Path
) -> None:
    """Four instants, four colours: the failure mode was all four being identical."""
    picks = {
        250: "red",
        1_250: "green",
        2_250: "blue",
        3_250: "white",
    }
    seen: list[tuple[int, int, int]] = []
    for offset_ms, expected in picks.items():
        pixel = _frame_pixel(colours_clip, offset_ms, tmp_path=tmp_path)
        seen.append(pixel)
        dominant = max(range(3), key=lambda index: pixel[index])
        wanted = {"red": 0, "green": 1, "blue": 2, "white": -1}[expected]
        if wanted >= 0:
            assert dominant == wanted, f"{offset_ms} ms should be {expected}, got {pixel}"
            # 120, not 200: ffmpeg writes limited-range yuv420p, so pure green decodes to about
            # (0, 127, 0). A threshold read off an RGB mental model would fail on the right frame.
            assert pixel[wanted] > 110, f"{offset_ms} ms is too dim for {expected}: {pixel}"
        else:
            # White: every channel high.
            assert min(pixel) > 200, f"{offset_ms} ms should be white, got {pixel}"
    assert len(set(seen)) == len(seen), f"instants returned duplicate frames: {seen}"


def test_a_seek_past_the_end_wraps_for_a_looped_clip(
    colours_clip: Path, tmp_path: Path
) -> None:
    """A looped segment several loops in must still land on the right frame.

    The plan can hand out an offset larger than the clip — ``-stream_loop -1`` wraps it, and
    ``-ss`` placed after the loop seeks into the *wrapped* timeline. 4 s clip, 5.25 s in → 1.25 s
    → green.
    """
    segment = BoxSegment(
        input_index=0, duration_ms=8000, kind=box_track.KIND_VIDEO, fit="contain", loop=True
    )
    offset_ms = 5_250
    chain = box_track.build_single(
        segment, source_offset_ms=offset_ms, width=160, height=90, input_seeked=True
    )
    target = tmp_path / "wrapped.png"
    completed = subprocess.run(
        [
            FFMPEG, "-nostdin", "-y", "-loglevel", "error",
            *box_track.input_args(segment, colours_clip, frames=192, seek_ms=offset_ms),
            "-filter_complex", chain.text_for(input_base=0),
            "-map", "[boxout]",
            "-frames:v", "1", "-update", "1", str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    pixel = Image.open(target).convert("RGB").getpixel((80, 45))
    assert pixel[1] > 110 and pixel[0] < 90, f"5.25 s into a 4 s loop should be green: {pixel}"

# --- the film's frame inside a **looped** segment (round 129, second bug) -


def test_the_trim_bound_sits_one_film_frame_back_not_an_epsilon() -> None:
    """The graph must ask for the frame that *contains* the grid line, not the next one.

    ``trim=start=t`` keeps the first frame whose PTS is at or after ``t``, which is ``ceil``; the
    film shows ``floor``. With an epsilon bound the two only agree when the source's frame grid and
    the film's coincide — true for a 24 fps clip, false for every other rate, and false again
    inside a loop because ``-stream_loop`` restarts the source grid at the stream's duration
    (292.249 s on the sample, which is not a multiple of 1/24 s).

    The bound therefore sits one **film** frame back: far enough to include a 25 fps frame that
    begins ~40 ms before the line, and still excluding the frame before that.
    """
    segment = BoxSegment(
        input_index=0, duration_ms=4000, kind=box_track.KIND_VIDEO, fit="contain", loop=True
    )
    chain = box_track.build_single(
        segment,
        source_offset_ms=3_620,
        width=160,
        height=90,
        input_seeked=True,
        keep_timestamps=True,
    )
    grid = box_track.frame_start_ms(3_620) / 1000
    bound = grid - 1.0 / box_track.FPS + 0.001
    assert f"trim=start={bound:.6f}" in chain.text, chain.text
    # The window this creates must exclude the previous film frame (41.67 ms) and include a frame
    # that begins up to 40 ms before the line (25 fps).
    assert bound > grid - 1.0 / 24
    assert bound < grid - 0.040


@pytest.fixture(scope="module")
def loop_grid_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """26 frames at 25 fps, red for the first 13 and green for the rest, with **no audio**.

    26 frames / 25 fps = 1.040 s, which is **not** a whole number of 24 fps frames (24.96). That
    is the point: inside a loop, `-stream_loop` restarts the source's frame grid at 1.04 s, so the
    film's 24 fps grid and the source's grid drift apart by 1.7 ms per loop — and after three loops
    they are far enough apart that a `1e-6` trim bound lands on the neighbouring frame.

    Two colours, not a luma ramp: the assertion is then a comparison, not a tolerance, and a
    limited-range yuv round trip cannot blur it into ambiguity.
    """
    if FFMPEG is None:
        pytest.skip("no ffmpeg on PATH")
    folder = tmp_path_factory.mktemp("loopgrid")
    clip = folder / "loopgrid.mp4"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=0.52",
            "-f", "lavfi", "-i", "color=c=lime:s=160x90:r=25:d=0.52",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "0", str(clip),
        ],
        check=True,
        capture_output=True,
    )
    return clip


def _looped_frame_pixel(clip: Path, offset_ms: int, *, tmp_path: Path) -> tuple[int, int, int]:
    """One still of a looped clip through the **exact** preview path (`-ss` + `-copyts` + trim)."""
    segment = BoxSegment(
        input_index=0, duration_ms=8000, kind=box_track.KIND_VIDEO, fit="contain", loop=True
    )
    chain = box_track.build_single(
        segment,
        source_offset_ms=offset_ms,
        width=160,
        height=90,
        input_seeked=True,
        keep_timestamps=True,
    )
    target = tmp_path / f"looped-{offset_ms}.png"
    completed = subprocess.run(
        [
            FFMPEG, "-nostdin", "-y", "-loglevel", "error",
            *box_track.input_args(segment, clip, frames=200, seek_ms=offset_ms),
            "-filter_complex", chain.text_for(input_base=0),
            "-map", "[boxout]",
            "-frames:v", "1", "-update", "1", str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return Image.open(target).convert("RGB").getpixel((80, 45))


def test_a_looped_segment_shows_the_films_frame_not_a_neighbour(
    loop_grid_clip: Path, tmp_path: Path
) -> None:
    """3.64 s into an 8 s looped stretch: which colour is on screen?

    The browser seeks to `offset mod period` = 520 ms, which is source frame 13 — the first green
    one. The still has to agree. Measured on this clip: the fixed bound gives **green**
    (0, 254, 0); the old `1e-6` bound gave **red** (253, 0, 0), i.e. one frame early, because the
    trim's `ceil` picked the frame before the film's `floor` once the two grids had drifted apart.

    On the sample project the same drift put the still **13.82 %** of the box away from the DOM
    stage at 980 950 ms — the last disagreement the P1 gate could not explain, and the reason the
    gate went from 19/20 to 20/20.
    """
    pixel = _looped_frame_pixel(loop_grid_clip, 3_640, tmp_path=tmp_path)
    assert pixel[1] > 180 and pixel[0] < 90, f"expected green (frame 13), got {pixel}"
