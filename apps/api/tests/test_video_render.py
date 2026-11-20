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
from animatter.media import box_track
from animatter.media.box_track import KIND_IMAGE, BoxSegment
from animatter.media.video import (
    CRF,
    FPS,
    PRESET,
    AvatarLayer,
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


def one_segment_chain(
    label: str, *, inputs: int = 1, placement: str = "box"
) -> box_track.BoxChain:
    """A chain with one segment, so the graph can be read without the chain's own noise."""
    # Two segments with distinct input indices when two inputs are asked for: a single segment
    # can only ever read input 0, so asking uild_chain for two would be contradictory.
    segments = [
        BoxSegment(input_index=index, duration_ms=1000, kind=KIND_IMAGE, placement=placement)
        for index in range(inputs)
    ]
    chain = box_track.build_chain(segments, width=1920, height=1080)
    assert chain.inputs == inputs
    return box_track.BoxChain(
        text=chain.text.replace("[boxout]", f"[{label}]"),
        x=chain.x,
        y=chain.y,
        width=chain.width,
        height=chain.height,
        sources=[],
        input_base=chain.input_base,
        inputs=inputs,
    )


def one_avatar() -> AvatarLayer:
    return AvatarLayer(
        path=Path("avatar.png"), x=0, y=0, width=100, height=100, is_video=False
    )


# --- 全屏块在最顶层 (§19.26) ---------------------------------------------


def test_a_full_frame_chain_is_overlaid_after_the_text() -> None:
    """The failure this pins is silent: overlay it before the text and the film still encodes,
    still contains the block, and simply has a title drawn on top of it."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        box=one_segment_chain("boxout"),
        avatars=[one_avatar()],
        text=Layer(path=Path("text.png"), x=0, y=0),
        top=one_segment_chain("topout", placement="full"),
    )
    assert graph.index("[withtext]") < graph.index("[topout]")
    assert "[withtext][topout]overlay" in graph


def test_the_full_frame_chain_goes_before_the_subtitle_burn() -> None:
    """Subtitles still burn on top of everything: they are the one layer that must stay legible."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=Path("/tmp/x.ass"),
        box=one_segment_chain("boxout"),
        top=one_segment_chain("topout", placement="full"),
    )
    assert graph.index("[topout]") < graph.index("ass=filename=")


def test_the_top_chain_reads_the_inputs_after_the_text() -> None:
    """Its `input_base` is derived from everything before it, so no count may drift silently.

    Sample shape: background + 2 artwork layers + a 2-input box chain + 1 avatar + the text PNG
    means the top chain's first input is 7.
    """
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[Layer(path=Path("a.png")), Layer(path=Path("b.png"))],
        subtitle_path=None,
        box=one_segment_chain("boxout", inputs=2),
        avatars=[one_avatar()],
        text=Layer(path=Path("text.png"), x=0, y=0),
        top=one_segment_chain("topout", placement="full"),
    )
    assert "[7:v]" in graph


def test_without_a_full_frame_chain_the_graph_is_unchanged() -> None:
    """Every existing project renders through exactly this shape."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        box=one_segment_chain("boxout"),
        avatars=[one_avatar()],
        text=Layer(path=Path("text.png"), x=0, y=0),
    )
    assert "topout" not in graph
    assert graph.endswith("[withtext]copy[vout]")


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
    """``veryfast`` measured 9.42 s/60 s at a third of ``ultrafast``'s size.

    24 fps, not 25 (round 118): the artwork and the archive the film is cut against are 24.
    The two chain builders must also agree about the rate, or ``concat`` duplicates a frame at
    every material boundary — a stutter exactly where the picture changes.
    """
    from animatter.media import box_track

    assert PRESET == "veryfast"
    assert CRF == "20"
    assert FPS == 24
    assert box_track.FPS == FPS


def test_a_still_puts_its_full_frame_segment_on_the_top_chain() -> None:
    """§19.26: the still and the film must agree about which layer a block is on.

    A still that drew a 全屏 block under an avatar while the export drew it over would be the
    preview lying — exactly the failure `_still_digest` and the "same filtergraph as the export"
    promise exist to prevent.
    """
    boxed = box_track.build_single(
        BoxSegment(input_index=0, duration_ms=1000, kind=KIND_IMAGE),
        source_offset_ms=0,
        width=1440,
        height=810,
        x=240,
        y=209,
        out_label="boxout",
    )
    top = box_track.build_single(
        BoxSegment(input_index=0, duration_ms=1000, kind=KIND_IMAGE, placement="full"),
        source_offset_ms=0,
        width=1920,
        height=1080,
        out_label="topout",
    )
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        box=boxed,
        avatars=[one_avatar()],
        text=Layer(path=Path("text.png"), x=0, y=0),
        top=top,
    )
    # Each label appears exactly twice: once where the chain defines it, once where the
    # overlay consumes it. Two chains sharing one label would put four occurrences of that
    # label and none of the other — the shape of the real ffmpeg failure.
    assert graph.count("[boxout]") == 2
    assert graph.count("[topout]") == 2
    assert graph.index("[withtext]") < graph.index("[topout]")


def test_a_still_encodes_png_with_the_measured_compression_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Level 1, not zlib's default 6, and **after** the output path is not yet written.

    Measured on a real 1920×1080 preview frame: the encode drops 330 → **217 ms** at PSNR `inf`
    (the pixels are identical; only the deflate effort changes), for +17 % bytes on a local temp
    artefact that is never delivered as a master.

    Pinned as a test because it is the single cheapest win in the preview path and it is invisible
    from the outside: every level produces a valid PNG, so nothing but this assertion notices when
    a refactor quietly drops the flag.
    """
    from animatter.media import video as video_media

    command: list[str] = []

    def capture(args: list[str], *, timeout: int = 7200) -> None:
        command.extend(args)
        # `render_still` insists on a non-empty target, so the fake ffmpeg has to create one.
        Path(args[-1]).write_bytes(b"x")

    monkeypatch.setattr(video_media, "_run", capture)
    background = tmp_path / "bg.png"
    background.write_bytes(b"x")
    video_media.render_still(
        "ffmpeg",
        target=tmp_path / "still.png",
        at_ms=0,
        width=1920,
        height=1080,
        background=background,
        layers=[],
        box_seek_ms=0,
    )
    assert "-compression_level" in command, command
    assert command[command.index("-compression_level") + 1] == "1"
    # The level has to be attached to the *output* PNG: ffmpeg treats it as a codec option, and
    # placing it before `-i` silently does nothing.
    assert command.index("-compression_level") > command.index("-map")
    assert command.index("-c:v") < command.index("-compression_level")
