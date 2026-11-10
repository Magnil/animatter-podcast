"""Gallery pacing and fit arithmetic (§19.3.1, §19.5).

Every function here is a division that has a version which looks right for 3 images and
2 seconds and drifts on real material. The tests use numbers that expose the drift: a
37-image gallery, a 7 s block, an odd output length.
"""

from __future__ import annotations

import itertools

import pytest

from animatter.core.video_box import (
    MIN_DWELL_MS,
    fit_geometry,
    gallery_length,
    is_too_fast,
    split_gallery,
)

# --- gallery splitting ---------------------------------------------------


def test_equal_split_of_a_divisible_block() -> None:
    segments = split_gallery([1, 1, 1, 1], start_ms=0, output_ms=8000)
    assert [segment.duration_ms for segment in segments] == [2000, 2000, 2000, 2000]


def test_segments_tile_the_block_exactly() -> None:
    """Sum of the parts must equal the whole, or the box content ends early.

    ``output // n * n`` leaves ``output % n`` unaccounted for; a cumulative round does not.
    """
    segments = split_gallery([1] * 7, start_ms=0, output_ms=1000)
    assert sum(segment.duration_ms for segment in segments) == 1000


def test_segments_are_contiguous() -> None:
    segments = split_gallery([1, 2, 3], start_ms=500, output_ms=6000)
    pairs = itertools.pairwise(segments)
    for previous, current in pairs:
        assert previous.end_ms == current.start_ms


def test_split_starts_at_the_given_offset() -> None:
    """Segments live in output time, and the block does not start at zero."""
    segments = split_gallery([1, 1], start_ms=12_345, output_ms=2000)
    assert segments[0].start_ms == 12_345
    assert segments[-1].end_ms == 14_345


def test_proportional_weights_are_honoured() -> None:
    segments = split_gallery([3, 1], start_ms=0, output_ms=8000)
    assert segments[0].duration_ms == 6000
    assert segments[1].duration_ms == 2000


def test_a_zero_weight_still_gets_a_turn() -> None:
    """A weight of 0 would make an image never appear while still counting as a turn."""
    segments = split_gallery([0, 1], start_ms=0, output_ms=2000)
    assert all(segment.duration_ms > 0 for segment in segments)
    assert len(segments) == 2


def test_empty_gallery_produces_nothing() -> None:
    assert split_gallery([], start_ms=0, output_ms=5000) == []


def test_zero_length_block_produces_nothing() -> None:
    assert split_gallery([1, 1], start_ms=1000, output_ms=0) == []


def test_37_images_over_7_seconds_still_tile() -> None:
    """A real gallery size: the rounding residue is largest exactly here."""
    segments = split_gallery([1] * 37, start_ms=0, output_ms=7000)
    assert len(segments) == 37
    assert sum(segment.duration_ms for segment in segments) == 7000


def test_no_segment_ends_past_the_block() -> None:
    segments = split_gallery([1] * 5, start_ms=0, output_ms=4999)
    assert segments[-1].end_ms == 4999


# --- the too-fast warning -----------------------------------------------


def test_too_fast_flags_a_flicker() -> None:
    segments = split_gallery([1] * 40, start_ms=0, output_ms=2000)
    assert is_too_fast(segments)


def test_too_fast_is_quiet_for_comfortable_pacing() -> None:
    assert not is_too_fast(split_gallery([1] * 4, start_ms=0, output_ms=8000))


def test_too_fast_threshold_is_used_on_every_segment() -> None:
    segments = split_gallery([1, 1], start_ms=0, output_ms=MIN_DWELL_MS * 2 - 2)
    assert is_too_fast(segments)


# --- the headline number -------------------------------------------------


def test_gallery_length_is_the_dwell() -> None:
    assert gallery_length(8, 16_000) == 2000


def test_gallery_length_of_an_empty_gallery_is_zero() -> None:
    assert gallery_length(0, 16_000) == 0


# --- fit geometry --------------------------------------------------------


def test_contain_fits_inside_and_letterboxes() -> None:
    # A 9:16 phone screenshot in a 16:9 box: height is the constraint.
    geometry = fit_geometry("contain", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert geometry.letterboxed
    assert geometry.visible_h == 810
    assert geometry.visible_w < 1440


def test_cover_fills_and_crops_instead() -> None:
    geometry = fit_geometry("cover", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert not geometry.letterboxed
    assert geometry.visible_w == 1440
    assert geometry.visible_h == 810


def test_cover_scales_more_than_contain() -> None:
    contain = fit_geometry("contain", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    cover = fit_geometry("cover", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert cover.scale > contain.scale


def test_a_matching_aspect_ratio_needs_no_letterbox() -> None:
    geometry = fit_geometry("contain", asset_w=1920, asset_h=1080, box_w=1440, box_h=810)
    assert not geometry.letterboxed
    assert geometry.visible_w == 1440
    assert geometry.visible_h == 810


def test_blur_fill_keeps_every_pixel_like_contain() -> None:
    """Its *foreground* is a contain; the backdrop is a separate cover branch."""
    blur = fit_geometry("blur_fill", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    contain = fit_geometry("contain", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert (blur.visible_w, blur.visible_h) == (contain.visible_w, contain.visible_h)


def test_stretch_ignores_the_aspect_ratio() -> None:
    geometry = fit_geometry("stretch", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert geometry.visible_w == 1440
    assert geometry.visible_h == 810


def test_an_unknown_fit_falls_back_to_contain() -> None:
    """Losing content silently on a typo is worse than letterboxing."""
    geometry = fit_geometry("nonsense", asset_w=1080, asset_h=1920, box_w=1440, box_h=810)
    assert geometry.visible_h == 810
    assert geometry.visible_w < 1440


@pytest.mark.parametrize("fit", ["contain", "cover", "blur_fill", "stretch", "unknown"])
def test_a_zero_asset_size_does_not_divide_by_zero(fit: str) -> None:
    geometry = fit_geometry(fit, asset_w=0, asset_h=0, box_w=1440, box_h=810)
    assert geometry.visible_w == 1440
    assert geometry.visible_h == 810
