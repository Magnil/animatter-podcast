"""Avatar overlays in the filtergraph, and the base-poster switch (P3, §19.5).

Everything here is a string check plus one real encode, because the failures worth pinning
are the ones that produce a *wrong picture* rather than an error:

* numbering an avatar input off by one asks for the wrong stream, and ffmpeg's complaint
  ("Error binding filtergraph inputs/outputs") names neither the avatar nor the film;
* overlaying avatars **before** the box would put the panel behind a full-bleed photo;
* starting from ``background.png`` instead of ``background_bare.png`` leaves the bright
  photo underneath a dim overlay, so a slot dims to *almost* dim — visible only in motion.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from animatter.core.video_avatar import Interval
from animatter.errors import AppError
from animatter.media.video import AvatarLayer, Layer, build_filtergraph

FFMPEG = shutil.which("ffmpeg")


def test_an_avatar_adds_one_overlay_and_one_input() -> None:
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        avatars=[AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249)],
    )
    assert graph.count("overlay=") == 1
    assert "[1:v]" in graph, "background is input 0, so the first avatar is input 1"


def test_avatar_input_numbers_follow_layers_and_box_sources() -> None:
    """The whole reason the base is derived instead of hard-coded."""
    from animatter.media import box_track

    chain = box_track.build_chain(
        [
            box_track.BoxSegment(
                input_index=0, duration_ms=1000, kind=box_track.KIND_IMAGE,
                fit="blur_fill", crop=None, loop=False,
            ),
        ],
        width=1440,
        height=810,
        x=240,
        y=209,
    )
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[Layer(path=Path("stroke.png"))],
        subtitle_path=None,
        box=chain,
        avatars=[
            AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249),
            AvatarLayer(path=Path("b.mov"), x=1712, y=200, width=236, height=249),
        ],
    )
    # background(0) + one layer(1) + one box source(2) → avatars are 3 and 4.
    assert "[3:v]" in graph
    assert "[4:v]" in graph


def test_avatars_sit_after_the_box_and_before_the_subtitles() -> None:
    """A panel dimmed *behind* a full-bleed photo is invisible; text must stay on top."""
    from animatter.media import box_track

    chain = box_track.build_chain(
        [
            box_track.BoxSegment(
                input_index=0, duration_ms=1000, kind=box_track.KIND_IMAGE,
                fit="blur_fill", crop=None, loop=False,
            ),
        ],
        width=1440,
        height=810,
        x=240,
        y=209,
    )
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=Path("subs.ass"),
        box=chain,
        avatars=[AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249)],
    )
    box_at = graph.index("[boxout]")
    avatar_at = graph.index("[av0img]")
    ass_at = graph.index("[av0]ass=")
    assert box_at < avatar_at < ass_at


def test_an_avatar_track_is_not_looped_and_not_truncated() -> None:
    """``eof_action=pass`` is the safety net: an early end shows the frame underneath."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        avatars=[AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249)],
    )
    assert "eof_action=pass:format=auto[av0]" in graph


def test_an_avatar_still_repeats_like_any_other_image() -> None:
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        avatars=[
            AvatarLayer(
                path=Path("lit.png"), x=42, y=200, width=236, height=249, is_video=False
            )
        ],
    )
    assert "eof_action=repeat" in graph


def test_an_avatar_is_scaled_to_the_slot_not_the_canvas() -> None:
    """The PNG is 236×249 and the slot is 177×188; scaling to 1920 would smear it."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        avatars=[AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249)],
    )
    assert "scale=236:249" in graph
    assert "overlay=x=42:y=200" in graph


def test_no_avatars_leaves_the_graph_exactly_as_before() -> None:
    """P1/P2 output must be byte-identical: this is an additive change."""
    with_none = build_filtergraph(
        width=1920, height=1080, layers=[], subtitle_path=None, avatars=None
    )
    with_empty = build_filtergraph(
        width=1920, height=1080, layers=[], subtitle_path=None, avatars=[]
    )
    assert with_none == with_empty
    assert "overlay=" not in with_none


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_a_real_encode_switches_the_avatar_at_the_right_frame(tmp_path: Path) -> None:
    """End to end through ffmpeg: lit for 1 s, dim for 1 s, checked by decoding frames.

    Not with ``-ss`` on the track: it is variable-frame-rate, and an input seek lands on the
    nearest frame boundary, which reads as an off-by-one. Resampling to 25 fps and indexing
    is the measurement that matches what the overlay sees.
    """
    from PIL import Image

    from animatter.core.video_avatar import TrackPiece
    from animatter.media.avatar_track import build_track

    lit = tmp_path / "lit.png"
    dim = tmp_path / "dim.png"
    Image.new("RGBA", (64, 64), (250, 250, 250, 255)).save(lit)
    Image.new("RGBA", (64, 64), (110, 110, 110, 255)).save(dim)
    background = tmp_path / "bg.png"
    Image.new("RGBA", (1920, 1080), (10, 10, 10, 255)).save(background)

    sequence = [TrackPiece(lit=True, duration_ms=1000), TrackPiece(lit=False, duration_ms=1000)]
    from animatter.media.avatar_track import pieces_of

    track = tmp_path / "track.mov"
    build_track(
        str(FFMPEG),
        target=track,
        pieces=pieces_of(lit=[lit], dim=dim, sequence=sequence),
        work_dir=tmp_path,
    )

    graph_target = tmp_path / "out.mp4"
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        avatars=[
            AvatarLayer(path=track, x=100, y=100, width=64, height=64),
        ],
    )
    script = tmp_path / "g.filtergraph"
    script.write_text(graph, encoding="utf-8")
    subprocess.run(
        [
            str(FFMPEG), "-nostdin", "-y", "-loglevel", "error",
            "-loop", "1", "-t", "2.000", "-i", str(background),
            "-i", str(track),
            "-filter_complex_script", str(script),
            "-map", "[vout]", "-r", "25", "-fps_mode", "cfr",
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
            str(graph_target),
        ],
        capture_output=True,
        check=True,
    )

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error",
            "-i", str(graph_target), str(frames_dir / "f-%03d.png"), "-y",
        ],
        capture_output=True,
        check=True,
    )
    frames = sorted(frames_dir.glob("f-*.png"))
    assert len(frames) >= 45

    def pixel(index: int) -> tuple[int, int, int]:
        return Image.open(frames[index]).convert("RGB").getpixel((120, 120))

    before = pixel(12)  # 0.48 s
    after = pixel(30)  # 1.20 s
    assert before[0] > 200, f"0.48 s should be lit, got {before}"
    assert after[0] < 150, f"1.20 s should be dim, got {after}"


def test_a_chunk_track_is_declared_without_a_time_bound(tmp_path: Path) -> None:
    """The whole-track declaration is bare `-i`: no `-ss`, no `-t` (§19.29).

    Both bounds are destructive on a track that already covers exactly the chunk:

    * `-ss` on the sparse whole-film clip delivered 7 of 298 frames;
    * `-t` cuts the terminator frame the concat demuxer uses to give the previous frame its
      duration, so the avatar stopped drawing 8 s before the chunk ended.

    `eof_action=pass` then shows the plate underneath, which is the reported "头像框消失".
    """
    background = tmp_path / "bg.png"
    background.write_bytes(b"x")
    track = tmp_path / "track.mov"
    track.write_bytes(b"x")
    seen: list[list[str]] = []

    def capture(command: list[str], *, timeout: int = 0) -> None:
        seen.append(command)

    video = importlib.import_module("animatter.media.video")
    original = video._run
    video._run = capture  # type: ignore[assignment]
    try:
        with pytest.raises(AppError):  # the fake ffmpeg writes no file, so the tail check fires
            video.render(
                "ffmpeg",
                target=tmp_path / "out.mp4",
                duration_ms=2000,
                width=1920,
                height=1080,
                background=background,
                layers=[],
                avatars=[AvatarLayer(path=track, x=10, y=10, width=64, height=64)],
                frames=48,
            )
    finally:
        video._run = original  # type: ignore[assignment]

    command = seen[0]
    position = command.index(str(track))
    # Walk back to the option group that introduced this input.
    start = position - 1
    while start > 0 and command[start - 1] != "-i":
        start -= 1
    assert command[position - 1] == "-i", command[max(0, position - 4):position + 1]
    group = command[start:position]
    assert "-ss" not in group, group
    assert "-t" not in group, group


def test_interval_import_is_used() -> None:
    """Keep the import honest: the tests above spell out the same type the app uses."""
    assert Interval(0, 1).duration_ms == 1
