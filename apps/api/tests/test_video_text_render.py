"""Tests for the text overlay's position in the filtergraph and the unbound-avatar fix."""

from __future__ import annotations

from pathlib import Path

from animatter.media import box_track
from animatter.media.video import AvatarLayer, Layer, build_filtergraph


def _chain():
    return box_track.build_chain(
        [
            box_track.BoxSegment(
                input_index=0,
                duration_ms=1000,
                kind=box_track.KIND_IMAGE,
                fit="blur_fill",
                crop=None,
                loop=False,
            ),
        ],
        width=1440,
        height=810,
        x=240,
        y=209,
    )


def test_the_text_overlay_comes_after_the_avatars() -> None:
    """An avatar's rect contains its own name plate, so text drawn first is covered.

    Measured on the sample: group 头像1 starts at y=200 and its name sits at y=202 — inside the
    group. With the text as an ordinary layer (drawn before the avatars) every name vanished
    from the film while still previewing fine whenever no avatar layer existed.
    """
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=Path("subs.ass"),
        avatars=[AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249)],
        text=Layer(path=Path("text.png")),
    )
    assert graph.index("[av0img]") < graph.index("[textimg]"), graph
    assert graph.index("[textimg]") < graph.index("ass="), graph


def test_the_text_input_number_follows_layers_box_and_avatars() -> None:
    """Derived, not hard-coded: the same trap the avatars had."""
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[Layer(path=Path("stroke.png"))],
        subtitle_path=None,
        box=_chain(),
        avatars=[
            AvatarLayer(path=Path("a.mov"), x=42, y=200, width=236, height=249),
            AvatarLayer(path=Path("b.mov"), x=1712, y=200, width=236, height=249),
        ],
        text=Layer(path=Path("text.png")),
    )
    # background(0) + one layer(1) + one box source(2) + two avatars(3, 4) → text is 5.
    assert "[5:v]" in graph, graph


def test_no_text_layer_means_no_extra_overlay() -> None:
    without = build_filtergraph(width=1920, height=1080, layers=[], subtitle_path=None)
    assert "textimg" not in without
    assert without.count("overlay=") == 0


def test_the_text_layer_gets_the_same_offset_as_the_picture() -> None:
    """The still preview shifts time instead of seeking, so every still input must shift alike.

    ``render_still`` asks for "the frame at 30 s" by moving the PTS, not with ``-ss`` (seeking
    resets PTS to 0 and ``ass`` would then draw nothing). A static overlay that did **not** get
    the same shift would be 30 s out of step with the picture it belongs on — and because the
    overlay is an infinite loop it would still render, just at the wrong content.
    """
    graph = build_filtergraph(
        width=1920,
        height=1080,
        layers=[],
        subtitle_path=None,
        offset_ms=6000,
        text=Layer(path=Path("text.png")),
    )
    text_line = next(part for part in graph.split(";") if "[textimg]" in part)
    base_line = next(part for part in graph.split(";") if "[base0]" in part)
    assert "setpts=PTS+6.000/TB" in text_line, text_line
    assert "setpts=PTS+6.000/TB" in base_line, base_line
