"""Avatar lit/dim arithmetic (§19.5).

Every function here is interval arithmetic over millisecond integers, and every plausible
mistake produces a *working* film that is wrong: an avatar lagging 200 ms behind the voice,
one that stays lit through the other person's question, or a track ending a few frames
before the film does — which shows up as a frozen last frame, not an error.

The three cases that actually occur on real projects, each pinned below: sentences
**overlap** after a boundary edit; the same speaker resumes after a one-second pause (and
the *other* slots must go dim in the gap); and a stretch was deleted, so a lit interval that
was entirely inside it must not survive.
"""

from __future__ import annotations

from animatter.core.video_avatar import (
    Interval,
    any_lit,
    invert,
    lit_at,
    merge,
    pieces_in,
    track_pieces,
)

# --- merging -------------------------------------------------------------


def test_a_single_interval_survives_unchanged() -> None:
    assert merge([Interval(1000, 2000)], total_ms=5000) == [Interval(1000, 2000)]


def test_intervals_are_sorted() -> None:
    merged = merge([Interval(3000, 4000), Interval(0, 1000)], total_ms=5000, bridge_ms=0)
    assert merged == [Interval(0, 1000), Interval(3000, 4000)]


def test_a_short_gap_is_bridged() -> None:
    """Breaths are 200–600 ms; flickering the avatar there looks like a glitch."""
    merged = merge([Interval(0, 1000), Interval(1300, 2000)], total_ms=5000)
    assert merged == [Interval(0, 2000)]


def test_a_long_gap_is_kept() -> None:
    merged = merge([Interval(0, 1000), Interval(3000, 4000)], total_ms=5000)
    assert merged == [Interval(0, 1000), Interval(3000, 4000)]


def test_overlapping_intervals_merge() -> None:
    merged = merge([Interval(0, 2000), Interval(1500, 3000)], total_ms=5000)
    assert merged == [Interval(0, 3000)]


def test_an_interval_inside_another_does_not_shrink_it() -> None:
    """``max`` on the end, not assignment: an inner interval must not shorten the outer."""
    merged = merge([Interval(0, 5000), Interval(1000, 2000)], total_ms=5000)
    assert merged == [Interval(0, 5000)]


def test_intervals_are_clipped_to_the_film() -> None:
    merged = merge([Interval(0, 9000)], total_ms=5000)
    assert merged == [Interval(0, 5000)]


def test_an_interval_entirely_past_the_end_is_dropped() -> None:
    assert merge([Interval(6000, 7000)], total_ms=5000) == []


def test_an_interval_entirely_before_zero_is_dropped() -> None:
    assert merge([Interval(-500, -100)], total_ms=5000) == []


def test_a_negative_stretch_is_clipped_not_kept() -> None:
    merged = merge([Interval(-500, 1000)], total_ms=5000)
    assert merged == [Interval(0, 1000)]


def test_no_intervals_means_no_lit_time() -> None:
    assert merge([], total_ms=5000) == []


def test_a_zero_length_film_has_no_intervals() -> None:
    assert merge([Interval(0, 1000)], total_ms=0) == []


# --- inversion -----------------------------------------------------------


def test_invert_gives_the_dim_stretches() -> None:
    dim = invert([Interval(2000, 3000)], total_ms=5000)
    assert dim == [Interval(0, 2000), Interval(3000, 5000)]


def test_invert_of_always_lit_is_empty() -> None:
    assert invert([Interval(0, 5000)], total_ms=5000) == []


def test_invert_of_never_lit_is_the_whole_film() -> None:
    assert invert([], total_ms=5000) == [Interval(0, 5000)]


def test_invert_of_a_leading_stretch_has_no_leading_gap() -> None:
    assert invert([Interval(0, 2000)], total_ms=5000) == [Interval(2000, 5000)]


# --- sequences -----------------------------------------------------------


def test_pieces_tile_the_whole_film() -> None:
    """A track one frame short freezes the last frame; one frame long pushes the film."""
    pieces = track_pieces([Interval(1000, 2000)], total_ms=5000)
    assert sum(piece.duration_ms for piece in pieces) == 5000


def test_pieces_alternate_dim_lit_dim() -> None:
    pieces = track_pieces([Interval(1000, 2000)], total_ms=5000)
    assert [(piece.lit, piece.duration_ms) for piece in pieces] == [
        (False, 1000),
        (True, 1000),
        (False, 3000),
    ]


def test_never_lit_is_one_long_dim_piece() -> None:
    pieces = track_pieces([], total_ms=5000)
    assert len(pieces) == 1
    assert not pieces[0].lit
    assert pieces[0].duration_ms == 5000


def test_always_lit_is_one_long_lit_piece() -> None:
    pieces = track_pieces([Interval(0, 5000)], total_ms=5000)
    assert len(pieces) == 1
    assert pieces[0].lit


def test_pieces_never_include_a_zero_length_one() -> None:
    pieces = track_pieces([Interval(0, 2000), Interval(2000, 4000)], total_ms=5000)
    assert all(piece.duration_ms > 0 for piece in pieces)


def test_two_turns_produce_five_pieces() -> None:
    """dim / lit / dim / lit / dim — the shape every two-speaker exchange has."""
    pieces = track_pieces(
        [Interval(1000, 2000), Interval(3000, 4000)], total_ms=5000
    )
    assert [piece.lit for piece in pieces] == [False, True, False, True, False]


# --- slices (the chunked export) -----------------------------------------


def test_a_slice_keeps_the_state_that_was_current_at_its_start() -> None:
    """A chunk that begins while someone is talking must start lit, not dip for one frame.

    The per-chunk track is rebuilt from the intervals (§19.29), so ``start_ms`` mid-interval has
    to keep that interval's state rather than producing a leading dim piece.
    """
    pieces = pieces_in([Interval(0, 4000)], start_ms=1500, end_ms=3000)
    assert [(piece.lit, piece.duration_ms) for piece in pieces] == [(True, 1500)]


def test_a_slice_is_as_long_as_the_chunk_and_no_longer() -> None:
    """The track must cover exactly the chunk: short leaves the overlay dead, long pushes PTS."""
    for start, end in ((0, 5000), (1234, 9876), (1000, 1000 + 1)):
        pieces = pieces_in([Interval(500, 9000)], start_ms=start, end_ms=end)
        assert sum(piece.duration_ms for piece in pieces) == end - start


def test_a_slice_rebases_its_turns_to_zero() -> None:
    pieces = pieces_in(
        [Interval(2000, 3000), Interval(4000, 5000)], start_ms=1000, end_ms=5000
    )
    assert [piece.lit for piece in pieces] == [False, True, False, True]
    assert sum(piece.duration_ms for piece in pieces) == 4000


def test_a_slice_that_ends_inside_a_lit_interval_stops_lit() -> None:
    pieces = pieces_in([Interval(0, 10_000)], start_ms=0, end_ms=1500)
    assert [(piece.lit, piece.duration_ms) for piece in pieces] == [(True, 1500)]


def test_an_empty_slice_has_no_pieces() -> None:
    """A zero-length chunk cannot make a track at all; `build_track` refuses empty input."""
    assert pieces_in([Interval(0, 5000)], start_ms=2000, end_ms=2000) == []


def test_a_slice_of_a_never_lit_slot_is_one_dim_piece() -> None:
    pieces = pieces_in([], start_ms=500, end_ms=2500)
    assert [(piece.lit, piece.duration_ms) for piece in pieces] == [(False, 2000)]


# --- point queries -------------------------------------------------------


def test_lit_at_is_half_open() -> None:
    lit = [Interval(1000, 2000)]
    assert lit_at(lit, 1000)
    assert lit_at(lit, 1999)
    assert not lit_at(lit, 2000), "the instant belongs to the next piece"


def test_lit_at_outside_every_interval_is_false() -> None:
    assert not lit_at([Interval(1000, 2000)], 500)


def test_any_lit_is_false_for_a_speaker_who_never_talks() -> None:
    """Half the slots on a 4-slot / 2-speaker template are exactly this."""
    assert not any_lit([], total_ms=5000)
    assert any_lit([Interval(0, 1)], total_ms=5000)
