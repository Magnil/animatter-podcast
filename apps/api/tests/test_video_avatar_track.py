"""Avatar track rendering: stills in, one RGBA clip out (§19.5).

The mechanism is the concat demuxer with per-file ``duration``: N stills become a
variable-frame-rate clip whose frames land at the requested instants. Two measured facts
are enforced here because both are silent when wrong:

* **no BOM in the concat list.** With one, ffmpeg says ``Line 1: unknown keyword 'file'``
  — which names the keyword, not the encoding, and sends you looking at the paths.
* **the last entry gets no duration.** A trailing duration makes ffmpeg emit a final frame
  past the end, so the track is a few milliseconds longer than the film and the muxed file
  runs longer than the audio.

The switch points are also asserted, because "roughly in the right place" is not a standard
an avatar can meet: an avatar that lights up 200 ms late reads as lag.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from animatter.core.video_avatar import Interval, track_pieces
from animatter.errors import AppError
from animatter.media.avatar_track import (
    CODEC,
    AvatarPiece,
    build_track,
    pieces_of,
    write_concat_list,
)

FFMPEG = shutil.which("ffmpeg")


def make_still(tmp_path: Path, name: str, colour: tuple[int, int, int]) -> Path:
    from PIL import Image

    path = tmp_path / name
    Image.new("RGBA", (64, 64), (*colour, 255)).save(path)
    return path


# --- the concat list -----------------------------------------------------


def test_the_list_has_no_bom(tmp_path: Path) -> None:
    """A BOM makes ffmpeg report ``unknown keyword 'file'`` — see the module docstring."""
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    script = write_concat_list(
        tmp_path / "list.txt", [AvatarPiece(lit, 1000), AvatarPiece(lit, 2000)]
    )
    raw = script.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.startswith(b"file ")


def test_the_list_ends_with_a_bare_file_line(tmp_path: Path) -> None:
    """The demuxer needs a following timestamp, or the last duration is ignored.

    Measured: without it a 3.00 s script produced a 2.56 s clip.
    """
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    script = write_concat_list(
        tmp_path / "list.txt", [AvatarPiece(lit, 1000), AvatarPiece(lit, 2000)]
    )
    lines = script.read_text(encoding="utf-8").strip().splitlines()
    assert lines[-1].startswith("file ")
    # The duplicate is the last piece again, and it carries no duration.
    assert lines[-1] == lines[2]


def test_durations_are_snapped_to_the_frame_grid(tmp_path: Path) -> None:
    """1.500 s becomes 1.520 at 25 fps: the demuxer rounds each duration up a frame."""
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    script = write_concat_list(
        tmp_path / "list.txt", [AvatarPiece(lit, 1500), AvatarPiece(lit, 500)]
    )
    durations = [
        line.split()[1]
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.startswith("duration")
    ]
    assert durations == ["1.520", "0.480"]


def test_snapping_is_cumulative_not_per_piece(tmp_path: Path) -> None:
    """Ten 103 ms pieces must not each round to 120 ms and drift a full second."""
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    pieces = [AvatarPiece(lit, 103)] * 10
    script = write_concat_list(tmp_path / "list.txt", pieces)
    durations = [
        float(line.split()[1])
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.startswith("duration")
    ]
    assert sum(durations) == pytest.approx(1.04, abs=0.001), "1030 ms → 1040 ms, not 1200"


def test_a_missing_piece_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        write_concat_list(tmp_path / "list.txt", [])


# --- the rendered clip ---------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_a_track_has_the_right_length_and_size(tmp_path: Path) -> None:
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    dim = make_still(tmp_path, "dim.png", (110, 110, 110))
    target = tmp_path / "track.mov"
    build_track(
        str(FFMPEG),
        target=target,
        pieces=[
            AvatarPiece(lit, 1000),
            AvatarPiece(dim, 1500),
            AvatarPiece(lit, 500),
        ],
        work_dir=tmp_path,
    )
    assert target.is_file()
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=width,height,codec_name",
            "-of", "default=nw=1", str(target),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    ).stdout
    assert f"codec_name={CODEC}" in out
    assert "width=64" in out
    assert "height=64" in out
    duration = float(
        next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("duration="))
    )
    assert 2.95 <= duration <= 3.10, f"expected ~3.0 s, got {duration}"


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_the_switch_happens_at_the_requested_instant(tmp_path: Path) -> None:
    """Lit for 1 s, dim for 1 s, checked on frames decoded in order.

    **Not** with ``-ss``: the track is variable-frame-rate, and an input seek lands on the
    frame boundary nearest the request, which reads as "still lit at 0.5 s". Resampling to
    the film's 25 fps and indexing frames is the measurement that matches what the overlay
    actually sees.
    """
    from PIL import Image

    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    dim = make_still(tmp_path, "dim.png", (110, 110, 110))
    target = tmp_path / "track.mov"
    build_track(
        str(FFMPEG),
        target=target,
        pieces=[AvatarPiece(lit, 1000), AvatarPiece(dim, 1000)],
        work_dir=tmp_path,
    )

    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error",
            "-i", str(target), "-vf", "fps=25", str(tmp_path / "r-%03d.png"), "-y",
        ],
        capture_output=True,
        check=False,
    )
    frames = sorted(tmp_path.glob("r-*.png"))
    assert len(frames) >= 45, f"expected ~51 frames, got {len(frames)}"

    def red_at(index: int) -> int:
        return int(Image.open(frames[index]).convert("RGB").getpixel((32, 32))[0])

    assert red_at(12) > 200, "0.48 s should still be lit"
    assert red_at(30) < 150, "1.20 s should be dim"


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_a_zero_length_piece_is_refused(tmp_path: Path) -> None:
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    with pytest.raises(AppError):
        build_track(
            str(FFMPEG),
            target=tmp_path / "t.mov",
            pieces=[AvatarPiece(lit, 0)],
            work_dir=tmp_path,
        )


# --- mapping the sequence onto files -------------------------------------


def test_pieces_map_lit_and_dim_to_their_files(tmp_path: Path) -> None:
    lit = make_still(tmp_path, "lit.png", (250, 250, 250))
    dim = make_still(tmp_path, "dim.png", (110, 110, 110))
    sequence = track_pieces([Interval(1000, 2000)], total_ms=3000)
    pieces = pieces_of(lit=[lit], dim=dim, sequence=sequence)
    assert [piece.path for piece in pieces] == [dim, lit, dim]
    assert [piece.duration_ms for piece in pieces] == [1000, 1000, 1000]


def test_a_never_lit_slot_produces_one_dim_piece(tmp_path: Path) -> None:
    dim = make_still(tmp_path, "dim.png", (110, 110, 110))
    pieces = pieces_of(lit=[], dim=dim, sequence=track_pieces([], total_ms=2000))
    assert len(pieces) == 1
    assert pieces[0].path == dim
    assert pieces[0].duration_ms == 2000
