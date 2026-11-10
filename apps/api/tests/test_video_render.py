"""Video rendering: filtergraph shape and the two measured ffmpeg traps.

Everything here is about strings, paths and options — not about pixels — because the
pixels are covered by the real-encode probe and by the template/screenshot checks.
The failures worth pinning are the ones that produce a *wrong file* rather than an
error:

* a Windows path with ``C:\\`` inside a filtergraph option splits the option list;
* ``-ss`` on a still resets PTS, so no cue covers frame 0 and the preview silently
  comes back with no subtitles at all (measured: 0 differing pixels);
* ``-loop 1`` without ``-t`` never terminates;
* a missing font substitutes silently, so the check has to happen before the encode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from animatter.errors import AppError
from animatter.media.video import (
    CRF,
    FPS,
    PRESET,
    Layer,
    build_filtergraph,
    escape_filter_path,
    mux,
    render,
    render_still,
)

# --- path escaping ------------------------------------------------------


def test_windows_drive_letter_is_escaped_for_filtergraph_options() -> None:
    """``:`` separates options inside a filter argument, so ``C:`` breaks the parse."""
    assert escape_filter_path(Path("C:/x/y.ass")) == "C\\:/x/y.ass"


def test_backslashes_are_never_emitted() -> None:
    # Forward slashes are accepted everywhere; keeping ``\`` would need double escaping.
    assert "\\" not in escape_filter_path(Path(r"C:\data\项目\字幕.ass")).replace("\\:", "")


def test_non_ascii_path_survives() -> None:
    escaped = escape_filter_path(Path("D:/data/播客/字幕.ass"))
    assert "播客" in escaped


def test_single_quotes_in_a_path_are_escaped() -> None:
    # The ``ass=filename='...'`` form would otherwise close the quote early.
    assert "\\'" in escape_filter_path(Path("/tmp/it's.ass"))


# --- graph shape --------------------------------------------------------


def test_graph_without_layers_or_subtitles_is_a_pass_through() -> None:
    graph = build_filtergraph(width=1920, height=1080, layers=[], subtitle_path=None)
    assert "copy[" in graph
    assert graph.rstrip().endswith("[vout]")


def test_each_layer_adds_one_overlay_and_one_input() -> None:
    layers = [Layer(path=Path("a.png")), Layer(path=Path("b.png"))]
    graph = build_filtergraph(width=1920, height=1080, layers=layers, subtitle_path=None)
    assert graph.count("overlay=") == 2
    assert "[1:v]" in graph and "[2:v]" in graph


def test_subtitle_burn_is_the_last_step() -> None:
    """Text must never end up under artwork."""
    layers = [Layer(path=Path("a.png"))]
    graph = build_filtergraph(
        width=1920, height=1080, layers=layers, subtitle_path=Path("/tmp/x.ass")
    )
    assert graph.rstrip().endswith("[vout]")
    last = graph.strip().split(";")[-1]
    assert "ass=filename=" in last


def test_setsar_is_applied_to_every_branch() -> None:
    """A SAR mismatch makes ``overlay`` rescale the layer behind the user's back."""
    layers = [Layer(path=Path("a.png"))]
    graph = build_filtergraph(
        width=1920, height=1080, layers=layers, subtitle_path=Path("/tmp/x.ass")
    )
    # One for the base, one for the layer. Both matter.
    assert graph.count("setsar=1") >= 2


def test_still_offset_is_written_in_seconds() -> None:
    """``PTS`` is in timebase units, so ``PTS+6000/TB`` would mean 6000 *seconds*."""
    graph = build_filtergraph(
        width=1920, height=1080, layers=[], subtitle_path=Path("/tmp/x.ass"), offset_ms=6000
    )
    assert "PTS+6.000/TB" in graph
    assert "+6000/TB" not in graph


def test_no_offset_means_no_setpts() -> None:
    graph = build_filtergraph(
        width=1920, height=1080, layers=[], subtitle_path=Path("/tmp/x.ass"), offset_ms=0
    )
    assert "setpts" not in graph


def test_layer_position_reaches_the_overlay() -> None:
    graph = build_filtergraph(
        width=1920, height=1080, layers=[Layer(path=Path("a.png"), x=240, y=209)], subtitle_path=None
    )
    assert "overlay=x=240:y=209" in graph


# --- encode preconditions ----------------------------------------------


def test_render_rejects_a_zero_duration(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        render(
            "ffmpeg",
            target=tmp_path / "out.mp4",
            duration_ms=0,
            width=1920,
            height=1080,
            background=tmp_path / "bg.png",
            layers=[],
        )
    assert excinfo.value.code == "video_duration_invalid"


def test_render_reports_an_uncompiled_template(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        render(
            "ffmpeg",
            target=tmp_path / "out.mp4",
            duration_ms=1000,
            width=1920,
            height=1080,
            background=tmp_path / "missing.png",
            layers=[],
        )
    assert excinfo.value.code == "template_not_compiled"


def test_render_names_the_layer_that_is_missing(tmp_path: Path) -> None:
    background = tmp_path / "bg.png"
    background.write_bytes(b"not really a png")
    with pytest.raises(AppError) as excinfo:
        render(
            "ffmpeg",
            target=tmp_path / "out.mp4",
            duration_ms=1000,
            width=1920,
            height=1080,
            background=background,
            layers=[Layer(path=tmp_path / "overlay.png")],
        )
    assert "overlay.png" in excinfo.value.message


def test_render_still_checks_the_font_before_encoding(tmp_path: Path) -> None:
    """A three-minute encode is far too expensive to discover a missing font at the end."""
    from tests.test_video_subtitles import first_available_font

    from animatter.media.subtitle_ass import SubtitleStyle, write_ass

    if first_available_font() is None:
        pytest.skip("这台机器上没有可用的中文字体")
    background = tmp_path / "bg.png"
    background.write_bytes(b"x")
    ass = write_ass(
        tmp_path / "x.ass",
        [],
        style=SubtitleStyle(font_name="Absolutely Not A Font 12345"),
    )
    with pytest.raises(AppError) as excinfo:
        render_still(
            "ffmpeg",
            target=tmp_path / "still.png",
            at_ms=0,
            width=1920,
            height=1080,
            background=background,
            layers=[],
            subtitles=ass,
        )
    assert excinfo.value.code == "subtitle_font_missing"


# --- mux ----------------------------------------------------------------


def test_mux_needs_both_inputs(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(AppError) as excinfo:
        mux("ffmpeg", video=video, audio=tmp_path / "a.m4a", target=tmp_path / "out.mp4")
    assert excinfo.value.code == "audio_missing"


def test_mux_reports_a_missing_video(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    with pytest.raises(AppError) as excinfo:
        mux("ffmpeg", video=tmp_path / "v.mp4", audio=audio, target=tmp_path / "out.mp4")
    assert excinfo.value.code == "video_missing"


# --- the constants that decide cost and quality ------------------------


def test_encoder_settings_are_the_measured_ones() -> None:
    """``veryfast`` measured 9.42 s/60 s at a third of ``ultrafast``'s size."""
    assert PRESET == "veryfast"
    assert CRF == "20"
    assert FPS == 25
