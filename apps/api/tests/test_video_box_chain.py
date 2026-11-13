"""The chain that turns segments into ONE overlay (§19.5, §19.11 陷阱 1).

The thing this file protects is not aesthetics: a wrong chain produces an mp4 that
encodes fine and looks wrong. Specifically —

* **input numbering** — the box chain does not own the ffmpeg input list; the film spends
  input 0 on the background and one per artwork layer. Off-by-one there draws the frame
  stroke as the box content, which looks like the template lost its artwork.
* **gap filling** — an overlay input must span the whole film or the overlay stops early
  and the frame freezes. Gaps are plate excerpts, and the total must equal the output
  length exactly.
* **label collision** — the same file can legitimately appear twice in a chain, so labels
  must come from the segment's *position*, not from its input number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from animatter.errors import AppError
from animatter.media.box_track import (
    KIND_IMAGE,
    KIND_PLATE,
    KIND_VIDEO,
    BoxSegment,
    build_canvas_chain,
    build_chain,
    build_single,
    input_args,
)


def segment(
    *,
    input_index: int = 0,
    duration_ms: int = 2000,
    kind: str = KIND_IMAGE,
    fit: str = "cover",
    **rest: object,
) -> BoxSegment:
    return BoxSegment(
        input_index=input_index,
        duration_ms=duration_ms,
        kind=kind,
        fit=fit,
        **rest,  # type: ignore[arg-type]
    )


# --- input declarations -------------------------------------------------


def test_a_still_is_looped_for_its_duration(tmp_path: Path) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    args = input_args(segment(duration_ms=3000), image)
    assert "-loop" in args
    assert "3.000" in args


def test_a_short_clip_is_stream_looped(tmp_path: Path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    args = input_args(segment(kind=KIND_VIDEO, duration_ms=9000, loop=True), clip)
    assert "-stream_loop" in args
    # A still must NOT get -stream_loop: it would make `-t` meaningless.
    assert "-loop" not in args


def test_a_long_clip_is_truncated_not_looped(tmp_path: Path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    args = input_args(segment(kind=KIND_VIDEO, duration_ms=2000, loop=False), clip)
    assert "-stream_loop" not in args
    assert "-t" in args


def test_a_missing_source_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(AppError) as caught:
        input_args(segment(), tmp_path / "nope.png")
    assert "nope.png" in caught.value.message


# --- the chain -----------------------------------------------------------


def test_one_segment_becomes_a_pass_through() -> None:
    chain = build_chain([segment()], width=1440, height=810)
    assert "null[boxout]" in chain.text
    assert "concat" not in chain.text


def test_two_segments_are_concatenated() -> None:
    chain = build_chain([segment(), segment(input_index=1)], width=1440, height=810)
    assert "concat=n=2:v=1:a=0[boxout]" in chain.text


def test_the_box_size_is_pushed_into_every_branch() -> None:
    chain = build_chain([segment(), segment(input_index=1)], width=1440, height=810)
    assert "scale=1440:810" in chain.text


def test_every_branch_has_an_explicit_pixel_aspect() -> None:
    """A missing ``setsar`` makes overlay silently rescale and the content lands off-box."""
    chain = build_chain(
        [segment(fit="cover"), segment(input_index=1, fit="contain"), segment(input_index=2, fit="stretch")],
        width=1440,
        height=810,
    )
    for line in chain.text.splitlines():
        if "scale=" in line or "crop=" in line or "pad=" in line:
            assert "setsar=1" in line or "overlay" in line, line


def test_input_base_offsets_every_reference() -> None:
    chain = build_chain([segment(input_index=0), segment(input_index=1)], width=800, height=450)
    rebased = chain.text_for(input_base=2)
    assert "[0:v]" not in rebased
    assert "[2:v]" in rebased
    assert "[3:v]" in rebased


def test_input_base_zero_changes_nothing() -> None:
    chain = build_chain([segment()], width=800, height=450)
    assert chain.text_for(input_base=0) == chain.text


def test_labels_come_from_position_not_input_number() -> None:
    """The same file twice would otherwise emit two filters with one label."""
    chain = build_chain(
        [segment(input_index=0), segment(input_index=0)], width=800, height=450
    )
    for index in range(2):
        # Each label is *defined* exactly once (and used once more by `concat`).
        assert chain.text.count(f"setpts=PTS-STARTPTS[bxs{index}]") == 1
    assert "concat=n=2" in chain.text


def test_blur_fill_splits_the_input() -> None:
    chain = build_chain([segment(fit="blur_fill")], width=1440, height=810)
    assert "split=2" in chain.text
    assert "boxblur" in chain.text


def test_blur_fill_labels_do_not_collide_across_two_segments() -> None:
    chain = build_chain(
        [segment(input_index=0, fit="blur_fill"), segment(input_index=1, fit="blur_fill")],
        width=1440,
        height=810,
    )
    assert "bgbg0" in chain.text and "bgbg1" in chain.text


def test_cover_keeps_the_top_of_the_picture() -> None:
    """Round 108: 「填满裁切」 must crop the bottom, not both ends.

    `crop` defaults to the centre, so a 9:16 screenshot in a 16:9 box lost its title bar
    *and* kept the empty bottom — the exact opposite of what the material needs.
    """
    chain = build_chain([segment(fit="cover")], width=1440, height=810)
    assert "crop=1440:810:0:0" in chain.text


def test_the_blurred_backdrop_is_anchored_at_the_top_too() -> None:
    """The backdrop is a `cover`: centring it there would also drift what shows behind."""
    chain = build_chain([segment(fit="blur_fill")], width=1440, height=810)
    assert "crop=1440:810:0:0" in chain.text


def test_blur_crop_scales_nothing() -> None:
    """The whole point of the mode: the picture keeps its own pixels.

    Every other strategy resizes; this one exists for material that must stay sharp, so a
    `scale=1440:810` on the foreground would defeat it.
    """
    chain = build_chain([segment(fit="blur_crop")], width=1440, height=810)
    # One chain is a `;`-joined filter sequence on a single line, so split on `;`.
    filters = [part for part in chain.text.split(";") if "bcimg" in part]
    assert filters
    for part in filters:
        assert "scale=" not in part, part


def test_blur_crop_anchors_at_the_middle_of_the_top_edge() -> None:
    """Top-*centre*: horizontal overflow is split evenly, vertical overflow is all at the
    bottom. Top-left would keep only the right side of a wide picture."""
    chain = build_chain([segment(fit="blur_crop")], width=1440, height=810)
    assert "overlay=(W-w)/2:0:format=auto" in chain.text


def test_blur_crop_fills_the_leftover_with_a_blurred_backdrop() -> None:
    chain = build_chain([segment(fit="blur_crop")], width=1440, height=810)
    assert "boxblur=luma_radius=20" in chain.text
    assert "eq=brightness=-0.10" in chain.text


def test_blur_crop_and_blur_fill_share_one_backdrop() -> None:
    """Two copies of that chain would drift into looking like two different features."""
    crop = build_chain([segment(fit="blur_crop")], width=1440, height=810).text
    fill = build_chain([segment(fit="blur_fill")], width=1440, height=810).text
    for part in (
        "scale=1440:810:force_original_aspect_ratio=increase",
        "crop=1440:810:0:0",
        "boxblur=luma_radius=20:luma_power=2",
        "eq=brightness=-0.10",
    ):
        assert part in crop, part
        assert part in fill, part


def test_blur_crop_splits_the_input_once() -> None:
    """Two filters off one input without `split` makes ffmpeg reuse a consumed frame."""
    chain = build_chain([segment(fit="blur_crop")], width=1440, height=810)
    assert chain.text.count("split=2") == 1


def test_blur_crop_labels_do_not_collide_across_two_segments() -> None:
    chain = build_chain(
        [segment(fit="blur_crop"), segment(input_index=1, fit="blur_crop")],
        width=1440,
        height=810,
    )
    assert "bcbase0" in chain.text and "bcbase1" in chain.text
    assert chain.text.count("[bcbase0]") == 2  # defined once, used once


def test_contain_pads_rather_than_crops() -> None:
    chain = build_chain([segment(fit="contain")], width=1440, height=810)
    assert "pad=1440:810" in chain.text
    assert "force_original_aspect_ratio=decrease" in chain.text


def test_cover_crops_rather_than_pads() -> None:
    chain = build_chain([segment(fit="cover")], width=1440, height=810)
    assert "crop=1440:810" in chain.text
    assert "force_original_aspect_ratio=increase" in chain.text


def test_a_plate_segment_crops_out_of_its_source() -> None:
    chain = build_chain(
        [segment(kind=KIND_PLATE, crop=(240, 209, 1440, 810))], width=1440, height=810
    )
    assert "crop=1440:810:240:209" in chain.text


def test_empty_chain_is_refused_with_a_reason() -> None:
    with pytest.raises(AppError) as caught:
        build_chain([], width=800, height=450)
    assert "素材框" in caught.value.message


def test_a_zero_length_segment_is_refused() -> None:
    with pytest.raises(AppError):
        build_chain([segment(duration_ms=0)], width=800, height=450)


def test_every_segment_is_trimmed_to_an_exact_frame_count() -> None:
    """``trim`` is what makes the output length exact rather than "close enough".

    A **frame count**, not a duration (round 118). `trim=duration` keeps the frames whose end
    lands within the duration, so a 1399 ms segment at 24 fps (33.58 frames) emitted 33 and the
    shortfall accumulated along the chain — a three-chunk export of the sample film put a
    picture boundary one frame earlier than a one-chunk export of the same film.
    ``end_frame`` is counted from two rounded cumulative boundaries, so the chain tiles the
    film's frame grid by construction: 1500 ms = 36 frames, 2500 ms = 60.
    """
    chain = build_chain(
        [segment(duration_ms=1500), segment(input_index=1, duration_ms=2500)],
        width=800,
        height=450,
    )
    assert "trim=end_frame=36" in chain.text
    assert "trim=end_frame=60" in chain.text
    assert chain.frames == [36, 60]


def test_a_segment_that_is_not_a_whole_number_of_frames_rounds_on_the_grid() -> None:
    """1399 ms is 33.58 frames and 1399 more is 67.15: 34 then 33, summing to the film's 67.

    The point is the *pair*: rounding each segment independently to 34 and 34 would make the
    chain a frame longer than the timeline, and every later boundary would slide.
    """
    chain = build_chain(
        [segment(duration_ms=1399), segment(input_index=1, duration_ms=1399)],
        width=800,
        height=450,
    )
    assert chain.frames == [34, 33]
    assert sum(chain.frames) == round(2798 * 24 / 1000)


# --- the still-preview variant ------------------------------------------


def test_single_segment_seeks_within_itself() -> None:
    chain = build_single(
        segment(duration_ms=4000), source_offset_ms=1250, width=1440, height=810
    )
    # Negative: ffmpeg starts the input at 0 and the wanted frame is that far in.
    assert "setpts=PTS-1.250/TB" in chain.text
    # A still is one frame, so the trim bounds it rather than measuring it: whatever the input
    # rate turns out to be, exactly the shifted frame survives.
    assert "trim=end_frame=1" in chain.text


def test_single_segment_at_zero_needs_no_shift() -> None:
    chain = build_single(segment(), source_offset_ms=0, width=1440, height=810)
    # ``PTS-STARTPTS`` is a legitimate filter; what must not appear is a *shift*.
    assert "PTS-0" not in chain.text
    assert "/TB" not in chain.text


def test_single_segment_never_emits_more_than_one_frame() -> None:
    """Seeking to the very end must still produce a frame, and only one.

    The old shape trimmed to a *duration* and had to clamp it to 1 ms so the trim was not empty;
    a frame count says the same thing directly and cannot be emptied by arithmetic.
    """
    chain = build_single(
        segment(duration_ms=2000), source_offset_ms=2000, width=1440, height=810
    )
    assert "trim=end_frame=1" in chain.text
    assert "trim=duration" not in chain.text


# --- 全屏 (round 111, §19.25) --------------------------------------------


def test_a_full_frame_segment_is_normalised_into_the_canvas() -> None:
    chain = build_canvas_chain(
        [segment(placement="full")], canvas_w=1920, canvas_h=1080, box_w=1440, box_h=810
    )
    assert "scale=1920:1080" in chain.text
    assert "pad=" not in chain.text


def test_a_box_segment_is_padded_into_the_canvas_at_the_box() -> None:
    """Its own fit is applied to the **box**, then the box is placed inside the canvas."""
    chain = build_canvas_chain(
        [segment(placement="box")],
        canvas_w=1920,
        canvas_h=1080,
        box_w=1440,
        box_h=810,
        box_x=240,
        box_y=209,
    )
    assert "scale=1440:810" in chain.text
    assert "pad=1920:1080:240:209:color=black@0" in chain.text


def test_the_canvas_chain_is_placed_at_the_origin() -> None:
    chain = build_canvas_chain(
        [segment(placement="full")], canvas_w=1920, canvas_h=1080, box_w=1440, box_h=810
    )
    assert (chain.x, chain.y) == (0, 0)
    assert (chain.width, chain.height) == (1920, 1080)


def test_the_canvas_chain_is_rgba() -> None:
    """The pad colour is transparent, which only means anything in an alpha format."""
    chain = build_canvas_chain(
        [segment(placement="box")], canvas_w=1920, canvas_h=1080, box_w=1440, box_h=810
    )
    assert "format=rgba" in chain.text


def test_a_mixed_canvas_chain_concatenates_both_kinds() -> None:
    chain = build_canvas_chain(
        [segment(placement="full"), segment(placement="box")],
        canvas_w=1920,
        canvas_h=1080,
        box_w=1440,
        box_h=810,
    )
    assert "concat=n=2" in chain.text
    assert "pad=1920:1080:0:0" in chain.text


def test_the_default_chain_is_untouched_by_the_new_builder() -> None:
    """A project with no 全屏 block must render through exactly the code it did before."""
    boxed = build_chain([segment(placement="box")], width=1440, height=810, x=240, y=209)
    assert "pad=" not in boxed.text
    assert "format=rgba" not in boxed.text
    assert (boxed.x, boxed.y, boxed.width, boxed.height) == (240, 209, 1440, 810)


def test_a_plate_inside_a_canvas_chain_is_padded_too() -> None:
    """The plate only exists inside the box, so in a full-frame chain it must be padded."""
    plate = segment(kind=KIND_PLATE, crop=(240, 209, 1440, 810), placement="box")
    chain = build_canvas_chain(
        [plate],
        canvas_w=1920,
        canvas_h=1080,
        box_w=1440,
        box_h=810,
        box_x=240,
        box_y=209,
    )
    assert "crop=1440:810:240:209" in chain.text
    assert "pad=1920:1080:240:209" in chain.text
