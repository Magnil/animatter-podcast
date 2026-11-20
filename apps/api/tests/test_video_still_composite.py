"""Still pre-compositing and the two memory levers (§19.28).

Three failures are worth pinning here, and all three are *silent*:

* a pre-composited segment that is scaled **again** is still a picture, just a blurrier one — the
  PNG is already at the chain's target size and any fit re-samples it;
* a video segment that gets pre-composited freezes on its first frame, because the PNG cannot
  change while the clip plays;
* a top chain that is skipped when it *does* draw something loses a 全屏 block, and the film just
  shows the plate over it.

The last test is a real encode: the whole point of the optimisation is that the picture is the
same, so it is compared frame by frame rather than argued about.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from animatter.media import box_track
from animatter.media import video as video_module
from animatter.media.box_track import KIND_IMAGE, KIND_TRANSPARENT, KIND_VIDEO, BoxSegment
from animatter.media.video import build_filtergraph, composite_still, render
from animatter.services.video_render import _needs_top_chain

FFMPEG = shutil.which("ffmpeg")


def _segment(kind: str = KIND_IMAGE, **kwargs: object) -> BoxSegment:
    fields: dict[str, object] = {"input_index": 0, "duration_ms": 1000, "kind": kind}
    fields.update(kwargs)
    return BoxSegment(**fields)  # type: ignore[arg-type]


# --- pre-composited segments are not fitted twice ------------------------


def test_a_precomposited_segment_is_not_scaled_again() -> None:
    """The PNG is already at the chain's size; a `scale` would re-sample a correct frame.

    That full-frame swscale is also exactly the per-frame cost the flag exists to remove, so
    "harmless but wasteful" is the wrong reading — it is the thing being optimised away.
    """
    live = box_track._normalise(
        _segment(fit="contain"), 1440, 810, input_base=0, tag=0
    )
    cut = box_track._normalise(
        _segment(fit="contain", precomposited=True), 1440, 810, input_base=0, tag=0
    )
    assert "scale=" in live
    assert "scale=" not in cut
    assert "pad=" not in cut
    # `setsar=1` stays: overlay honours SAR, and the PNG carries none.
    assert cut == "[0:v]setsar=1"


def test_with_still_sources_marks_stills_and_keeps_videos_live() -> None:
    """A video segment must keep its own kind: one PNG cannot play a clip."""
    segments = [
        _segment(KIND_IMAGE, fit="blur_fill"),
        _segment(KIND_VIDEO, fit="contain", loop=True, input_index=1),
    ]
    sources = [Path("photo.jpg"), Path("clip.mp4")]
    png = Path("still-00.png")
    out, ordered = box_track.with_still_sources(segments, sources, {0: png})

    assert ordered == [png, Path("clip.mp4")]
    assert out[0].precomposited
    assert out[0].kind == KIND_IMAGE
    # Both point at their own new input number, in first-appearance order.
    assert (out[0].input_index, out[1].input_index) == (0, 1)
    assert not out[1].precomposited
    assert (out[1].kind, out[1].fit, out[1].loop) == (KIND_VIDEO, "contain", True)


def test_with_still_sources_leaves_placeholders_alone() -> None:
    """A placeholder is a `color` filter with no file: pre-compositing one would add an input."""
    segments = [_segment(KIND_TRANSPARENT), _segment(KIND_IMAGE)]
    out, ordered = box_track.with_still_sources(
        segments, [Path("photo.jpg")], {1: Path("still-01.png")}
    )
    assert out[0] == segments[0]
    assert ordered == [Path("still-01.png")]


def test_a_chain_of_precomposited_segments_has_no_blur_left() -> None:
    """The measured cost was `boxblur` on every frame; after this it must not be in the graph."""
    segments = [_segment(KIND_IMAGE, fit="blur_fill", precomposited=True)]
    chain = box_track.build_canvas_chain(
        segments, canvas_w=192, canvas_h=108, box_w=144, box_h=81, box_x=24, box_y=20
    )
    assert "boxblur" not in chain.text
    assert "pad=" not in chain.text


# --- the top chain is only built when it draws something ------------------


def test_an_all_placeholder_top_chain_is_not_built() -> None:
    assert not _needs_top_chain([_segment(KIND_TRANSPARENT), _segment(KIND_TRANSPARENT)])
    assert _needs_top_chain([_segment(KIND_TRANSPARENT), _segment(KIND_IMAGE, placement="full")])


# --- the thread cap is a global option, and only when asked --------------


def _captured_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: object) -> list[str]:
    from animatter.media import video as video_module

    background = tmp_path / "bg.png"
    background.write_bytes(b"x")
    target = tmp_path / "out.mp4"
    captured: list[str] = []

    def fake_run(
        command: list[str],
        *,
        timeout: int = 7200,
        cancel: object = None,
    ) -> None:
        captured.extend(command)
        target.write_bytes(b"x")

    monkeypatch.setattr(video_module, "_run", fake_run)
    render(
        "ffmpeg",
        target=target,
        duration_ms=1000,
        width=1920,
        height=1080,
        background=background,
        layers=[],
        **kwargs,  # type: ignore[arg-type]
    )
    return captured


def test_the_filter_thread_cap_is_emitted_as_a_global_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ffmpeg reads `-filter_complex_threads` as a *global*, so it must precede the first `-i`.

    Putting it after an input makes ffmpeg read it as that input's option and reject the run with
    a message about the input file, which reads like a missing asset.
    """
    command = _captured_command(monkeypatch, tmp_path, filter_threads=4)
    index = command.index("-filter_complex_threads")
    assert command[index + 1] == "4"
    assert index < command.index("-i")


def test_no_thread_cap_is_emitted_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The still preview and the tests keep ffmpeg's own default; only the export caps it."""
    assert "-filter_complex_threads" not in _captured_command(monkeypatch, tmp_path)


# --- the picture is the same ---------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="这台机器上没有 ffmpeg")
def test_a_composited_still_renders_the_same_picture_as_the_live_chain(
    tmp_path: Path,
) -> None:
    """End to end: the same segment through both paths, compared pixel by pixel.

    Deliberately **lossless** on both sides (``render_still`` writes a PNG): comparing two x264
    encodes would hide a mismatch inside encoder noise, and the number this asserts is the one
    that says "the optimisation did not change the picture". A small canvas and a real
    ``blur_fill`` keep the measured code path (split, boxblur, crop, overlay, pad) without paying
    for 1080p. The photo is a gradient rather than a flat colour, because blur and crop are no-ops
    on a constant image — a chain that skipped them would still "match".
    """
    from PIL import Image

    canvas_w, canvas_h, box_w, box_h, box_x, box_y = 192, 108, 144, 81, 24, 20
    photo = tmp_path / "photo.jpg"
    image = Image.new("RGB", (320, 200))
    for y in range(200):
        for x in range(320):
            image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    image.save(photo, quality=95)
    background = tmp_path / "bg.png"
    Image.new("RGBA", (canvas_w, canvas_h), (12, 12, 12, 255)).save(background)
    stroke = tmp_path / "stroke.png"
    Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0)).save(stroke)

    segment = _segment(KIND_IMAGE, fit="blur_fill")

    live_chain = box_track.build_canvas_chain(
        [segment],
        canvas_w=canvas_w, canvas_h=canvas_h,
        box_w=box_w, box_h=box_h, box_x=box_x, box_y=box_y,
    )
    live = tmp_path / "live.png"
    video_module.render_still(
        str(FFMPEG),
        target=live,
        at_ms=0,
        width=canvas_w,
        height=canvas_h,
        background=background,
        layers=[],
        box=live_chain,
        box_segment=segment,
        box_source=photo,
    )

    still = tmp_path / "still.png"
    composite_still(
        str(FFMPEG),
        target=still,
        width=canvas_w,
        height=canvas_h,
        segment=segment,
        source=photo,
        box_w=box_w,
        box_h=box_h,
        box_x=box_x,
        box_y=box_y,
    )
    cut_segments, cut_sources = box_track.with_still_sources([segment], [photo], {0: still})
    cut_chain = box_track.build_canvas_chain(
        cut_segments,
        canvas_w=canvas_w, canvas_h=canvas_h,
        box_w=box_w, box_h=box_h, box_x=box_x, box_y=box_y,
    )
    cut = tmp_path / "cut.png"
    video_module.render_still(
        str(FFMPEG),
        target=cut,
        at_ms=0,
        width=canvas_w,
        height=canvas_h,
        background=background,
        layers=[],
        box=cut_chain,
        box_segment=cut_segments[0],
        box_source=cut_sources[0],
    )

    a = Image.open(live).convert("RGB")
    b = Image.open(cut).convert("RGB")
    differences = [
        absolute
        for pa, pb in zip(a.getdata(), b.getdata(), strict=True)
        for left, right in zip(pa, pb, strict=True)
        for absolute in (abs(left - right),)
    ]
    mse = sum(value * value for value in differences) / len(differences)
    # Identical is a *pass*, and it happens: whether the yuv -> rgba round trip costs a bit on
    # this canvas depends on which frames ffmpeg's PNG encoder happened to see, so mse can be
    # exactly 0. Guarding it is not politeness — `255*255/0` is a ZeroDivisionError, i.e. the
    # test would fail hardest on the best possible outcome.
    psnr = math.inf if mse == 0 else math.log10(255 * 255 / mse) * 10
    # Not bit-identical in general, and it cannot be: the piece is written as an 8-bit **RGBA
    # PNG**, so the chain's internal yuv -> rgba conversion becomes a round trip through RGB,
    # while the live chain stays in yuv the whole way. `boxblur` is also slice-threaded, so even
    # two runs of the *same* live graph differ by a few units along a slice edge — which is why
    # this asserts PSNR against the bar the film was accepted at (§19.28), not equality.
    assert psnr >= 45, f"pre-composited frame is only {psnr:.1f} dB against the live chain"
    assert max(differences) <= 12, "a channel moved by more than an 8-bit round trip"


def test_the_still_is_composited_at_the_size_the_chain_will_draw_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The composite's target size depends on which chain the film uses, and both exist.

    A **split** film (one 全屏 block somewhere) draws its chain canvas-sized with the box padded
    transparently, so the PNG has to be the canvas or the margin is lost. A film with no 全屏
    block draws the opaque box-sized chain, and there a canvas-sized PNG would be scaled *again*
    by the overlay — the double fit `precomposited` exists to avoid. Neither mistake errors; both
    just render the still into the wrong rectangle.
    """
    from animatter.config import Settings
    from animatter.services import video_render
    from animatter.services.video_box import BoxPlan, BoxSplit

    sizes: list[tuple[int, int, int, int]] = []

    def fake(ffmpeg: str, **kwargs: object) -> Path:
        sizes.append(
            (
                kwargs["width"],  # type: ignore[arg-type]
                kwargs["height"],  # type: ignore[arg-type]
                kwargs["box_x"],  # type: ignore[arg-type]
                kwargs["box_y"],  # type: ignore[arg-type]
            )
        )
        target = kwargs["target"]
        assert isinstance(target, Path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        return target

    monkeypatch.setattr(video_module, "composite_still", fake)
    settings = Settings(data_dir=tmp_path)
    still = _segment(KIND_IMAGE)
    video = _segment(KIND_VIDEO, input_index=1)
    # A video segment must be left live: no PNG can play a clip, and compositing one would
    # silently freeze the picture on its first frame.
    mixed = [still, video]

    def plan(*, split: bool) -> BoxPlan:
        if not split:
            return BoxPlan(
                segments=mixed, sources=[Path("photo.jpg"), Path("clip.mp4")],
                width=144, height=81, box_x=24, box_y=20,
            )
        return BoxPlan(
            segments=mixed, sources=[Path("photo.jpg"), Path("clip.mp4")],
            width=144, height=81, box_x=24, box_y=20,
            split=BoxSplit(
                lower=(mixed[0], video),
                lower_sources=(Path("photo.jpg"), Path("clip.mp4")),
                upper=(_segment(KIND_TRANSPARENT), _segment(KIND_TRANSPARENT)),
                upper_sources=(),
            ),
        )

    cut = video_render._composite_stills(
        settings, plan(split=True), mixed, [Path("photo.jpg"), Path("clip.mp4")],
        work_dir=tmp_path / "split", tag="a",
    )
    assert list(cut) == [0], "only the still is composited, not the video"
    # Canvas-sized (BoxPlan's default canvas) with the box's own offset, because the split chain
    # pads to the canvas at that offset and the PNG has to line up with it.
    assert sizes[-1] == (1920, 1080, 24, 20)

    plain = video_render._composite_stills(
        settings, plan(split=False), mixed, [Path("photo.jpg"), Path("clip.mp4")],
        work_dir=tmp_path / "plain", tag="b",
    )
    assert list(plain) == [0]
    assert sizes[-1] == (144, 81, 0, 0)


def test_the_graph_the_composite_chain_builds_is_the_export_graph() -> None:
    """The preview and the film go through one builder (§19.7); this keeps that true.

    A pre-composited chain must reach `build_filtergraph` as an ordinary chain — no new argument,
    no separate branch — or the still preview would keep drawing the old path.
    """
    segments = [_segment(KIND_IMAGE, precomposited=True)]
    chain = box_track.build_canvas_chain(
        segments, canvas_w=192, canvas_h=108, box_w=144, box_h=81, box_x=24, box_y=20
    )
    graph = build_filtergraph(
        width=192,
        height=108,
        layers=[],
        subtitle_path=None,
        box=chain,
    )
    # background is input 0, so the chain's one input is 1; no `scale`/`boxblur` survives.
    assert "[1:v]setsar=1,format=rgba" in graph
    assert "boxblur" not in graph
    assert "[boxout]" in graph
