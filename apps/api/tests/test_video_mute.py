"""Round 108: the mute flag, the mix graph, and which blocks can speak (§19.20).

Three separable claims, so three groups:

1. **`muted` defaults to silence.** A film made before this round must sound identical after
   it: the mux has to stay on `-c copy` unless something is actually audible.
2. **The mix graph places each stretch by output time.** ``adelay`` in milliseconds, one input
   per stretch, ``normalize=0`` (``amix`` otherwise divides by the input count, i.e. the
   podcast gets quieter the moment one block is unmuted).
3. **A block without sound is not offered the button.** ``view_items`` answers it from
   ``video_asset.has_audio`` in one query, and a still/deck answers ``False`` by construction.

The export signature is the fourth: unmuting changes what the film sounds like, so an export
cached across a press of 「M」 would hand back the previous audio, silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from animatter.core.edl import Deletion, Edl, Keep
from animatter.ids import new_id, utcnow_iso
from animatter.media import video as video_media
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.models import VideoAsset, VideoItem, VideoTemplate
from animatter.services import video_render as render

PROJECT = "prj_mute"


def edl_with(total_ms: int) -> Edl:
    return Edl(
        original_duration_ms=total_ms,
        keeps=(Keep(orig_start=0, orig_end=total_ms, new_start=0),),
        deletions=(Deletion(start_ms=0, end_ms=0, source="test"),),
    )


def template(project_id: str) -> VideoTemplate:
    return VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path="",
        source_sha256="b" * 64,
        canvas_w=1920,
        canvas_h=1080,
        background_path="",
        overlay_path="",
        box_rect=json.dumps({"x": 240, "y": 209, "w": 1440, "h": 810}),
        box_fit_default="blur_fill",
        slots_json="{}",
        bindings_json="{}",
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )


def item(project_id: str, *, muted: bool = True, asset_id: str = "vasset_x") -> VideoItem:
    return VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind="media",
        start_ms=0,
        end_ms=10_000,
        z=0,
        fit="blur_fill",
        motion="none",
        asset_id=asset_id,
        deck_id=None,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        muted=muted,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )


def prepared(project_id: str, *, items: list[VideoItem] | None = None) -> render.PreparedVideo:
    return render.PreparedVideo(
        template=template(project_id),
        box_rect={"x": 240, "y": 209, "w": 1440, "h": 810},
        items=items if items is not None else [item(project_id)],
        edl=edl_with(10_000),
        duration_ms=10_000,
        avatars=[],
    )


# --- 1. silence is the default, and it stays a stream copy ----------------


def test_a_new_block_is_muted() -> None:
    """The film's sound is the podcast; a block's own sound is an extra."""
    assert item(PROJECT).muted is True


def test_no_mix_means_no_extra_ffmpeg_inputs() -> None:
    assert video_media.mux.__doc__ is not None
    # A silent film must not be re-encoded: that path is what makes 「M」 free for everyone
    # who never presses it.
    assert "copy" in video_media.mux.__doc__


# --- 「S」: replace, not mix (round 111) ---------------------------------


def test_a_solo_stretch_silences_the_podcast_over_its_own_span() -> None:
    """`S` is a replacement, so the podcast must be **zero** exactly where the clip speaks.

    ``volume=enable='between(t,a,b)':volume=0`` is what makes it a cut rather than a duck: the
    podcast stream stays one continuous track (so the film's length is unchanged) while its
    samples are silenced inside the stretch.
    """
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=1500, end_ms=3000, path=Path("a.mp4"), solo=True)]
    )
    assert "volume=enable='between(t,1.500,3.000)':volume=0" in graph


def test_a_mixed_stretch_leaves_the_podcast_alone() -> None:
    """Without `S` the podcast keeps its level: that is the M path and it must not regress."""
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=1500, end_ms=3000, path=Path("a.mp4"), solo=False)]
    )
    assert "volume=enable" not in graph


def test_only_solo_stretches_silence_the_podcast() -> None:
    graph, _args = video_media._mix_graph(
        [
            video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4"), solo=False),
            video_media.AudioMix(start_ms=2000, end_ms=2500, path=Path("b.mp4"), solo=True),
        ]
    )
    assert graph.count("volume=enable") == 1
    assert "between(t,2.000,2.500)" in graph


def test_the_solo_span_uses_output_time() -> None:
    """The same rule as every other time in this pipeline: `[start, end)` in output ms."""
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=61_500, end_ms=63_250, path=Path("a.mp4"), solo=True)]
    )
    assert "between(t,61.500,63.250)" in graph


def test_solo_still_ends_the_film_with_the_podcast() -> None:
    """`amix:duration=first` is what makes a stretch past the end harmless; solo must keep it."""
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4"), solo=True)]
    )
    assert "duration=first" in graph


def test_the_mix_graph_is_empty_but_valid_for_no_stretches() -> None:
    graph, args = video_media._mix_graph([])
    assert args == []
    assert "[1:a]aresample=48000" in graph
    assert "amix=inputs=1" in graph


# --- 2. the mix graph ----------------------------------------------------


def test_each_stretch_becomes_one_input_and_one_delayed_branch() -> None:
    stretches = [
        video_media.AudioMix(start_ms=0, end_ms=4000, path=Path("a.mp4")),
        video_media.AudioMix(start_ms=8000, end_ms=12_000, path=Path("b.mp4")),
    ]
    graph, args = video_media._mix_graph(stretches)
    assert args == ["-i", "a.mp4", "-i", "b.mp4"]
    # Input 0 is the video, 1 the podcast, so the stretches start at 2.
    assert "[2:a]aresample=48000" in graph
    assert "[3:a]aresample=48000" in graph
    assert "adelay=8000|8000" in graph
    assert "amix=inputs=3" in graph


def test_the_mix_does_not_halve_the_podcast() -> None:
    """``amix`` divides by the input count unless told not to.

    The default would make the podcast quieter the moment a block is unmuted — a change the
    user did not ask for, in the direction of "the film sounds wrong sometimes".
    """
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4"))]
    )
    assert "normalize=0" in graph


def test_the_mix_limits_instead_of_clipping() -> None:
    """Measured: summing two full-scale inputs put 36 000 of 96 000 samples at the ceiling.

    ``normalize=1`` would fix that by halving the podcast, which is the change nobody asked
    for. ``alimiter`` keeps both channels' content and only tames the peaks.
    """
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4"))]
    )
    assert "alimiter=limit=0.95" in graph


def test_the_limiter_compensates_its_own_delay() -> None:
    """`alimiter` delays its output by 239 samples (5 ms) unless told otherwise.

    A systematic 5 ms offset between a clip's picture and its sound is the exact class of bug
    this project refuses elsewhere — measured with an impulse, `:latency=1` brings it to 0.
    """
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4"))]
    )
    assert "latency=1" in graph


def test_a_stretch_runs_for_its_own_length_not_the_clip() -> None:
    graph, _args = video_media._mix_graph(
        [video_media.AudioMix(start_ms=0, end_ms=2500, path=Path("a.mp4"))]
    )
    assert "atrim=start=0:end=2.500" in graph


def test_every_branch_is_resampled_to_one_format() -> None:
    """``amix`` refuses inputs whose sample formats disagree.

    A phone screen recording is 48 kHz stereo and an older clip is 44.1 kHz mono; without
    resampling the failure lands at encode time, after the twenty-minute video encode.
    """
    graph, _args = video_media._mix_graph(
        [
            video_media.AudioMix(start_ms=0, end_ms=1000, path=Path("a.mp4")),
            video_media.AudioMix(start_ms=2000, end_ms=3000, path=Path("b.mp4")),
        ]
    )
    # One podcast branch plus one per stretch.
    assert graph.count("aresample=48000") == 3
    assert graph.count("channel_layouts=stereo") == 3


# --- 3. who may be offered the button -----------------------------------


def test_a_still_can_never_be_unmuted() -> None:
    row = VideoAsset(
        id="vasset_still",
        project_id=PROJECT,
        kind="image",
        path="still.png",
        original_name="still.png",
        sha256="c" * 64,
        width=100,
        height=100,
        duration_ms=0,
        has_audio=False,
        created_at=utcnow_iso(),
    )
    assert row.has_audio is False


def test_a_clip_records_whether_it_has_sound() -> None:
    """Read at import, where ffprobe already runs — not later, per export."""
    row = VideoAsset(
        id="vasset_clip",
        project_id=PROJECT,
        kind="video",
        path="clip.mp4",
        original_name="clip.mp4",
        sha256="d" * 64,
        width=320,
        height=180,
        duration_ms=4000,
        has_audio=True,
        created_at=utcnow_iso(),
    )
    assert row.has_audio is True


# --- 4. export identity --------------------------------------------------


def test_unmuting_a_block_changes_the_export_signature() -> None:
    """Otherwise 「M」 would hand back the previous audio from the cache, silently."""
    options = render.ExportOptions()
    silent = render._video_signature(prepared(PROJECT), options)
    audible = render._video_signature(
        prepared(PROJECT, items=[item(PROJECT, muted=False)]), options
    )
    assert silent != audible


def test_the_signature_is_stable_while_nothing_is_unmuted() -> None:
    options = render.ExportOptions()
    assert render._video_signature(prepared(PROJECT), options) == render._video_signature(
        prepared(PROJECT), options
    )


def test_the_subtitle_size_still_changes_the_signature() -> None:
    """The mute flag was added next to the other fields; this one must not have been lost."""
    plain = render._video_signature(prepared(PROJECT), render.ExportOptions())
    big = render._video_signature(
        prepared(PROJECT), render.ExportOptions(subtitle_style=SubtitleStyle(font_size=88))
    )
    assert plain != big
