"""The DOM preview's plan: what it promises, and the two traps it exists to avoid (§19.31.2).

Nothing here renders a video. What is pinned is the **contract** between the plan and the
browser, because every failure in this area produces a plausible picture rather than an error:

* a plan whose segments are numbered by the *box plan's* index leaves gaps at the dropped plate
  stretches, and the browser shows the wrong segment for the next 40 seconds;
* a cue band one line too short clips a descender, which reads as a font bug;
* an unbound avatar slot that still gets an element draws the bright poster photo over the
  empty frame the export leaves — the exact bug §19.5's `background_bare` switch fixes;
* a plan that puts a *path* in the payload hands the browser the file system (§8).

The layer ORDER is checked by the browser walkthrough (`scripts/check-p1-preview.mjs`), not
here: it is a property of the DOM, and a string test cannot see it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.ids import new_id, utcnow_iso
from animatter.media.subtitle_ass import SubtitleStyle, wrap_cjk
from animatter.models import VideoAsset, VideoItem, VideoTemplate
from animatter.services import video_plan

FFMPEG = shutil.which("ffmpeg")

#: Two avatar slots; only one is bound to a speaker by the fixtures below.
SLOTS = {
    "avatars": [
        {
            "name": "头像1",
            "label": "彼方",
            "rect": {"x": 42, "y": 200, "w": 177, "h": 188},
            "lit_path": "",
            "dim_path": "",
        },
        {
            "name": "头像2",
            "label": "马小褂",
            "rect": {"x": 42, "y": 417, "w": 177, "h": 187},
            "lit_path": "",
            "dim_path": "",
        },
    ],
    "texts": [],
    "notes": [],
}


def make_template(
    session: Session,
    project_id: str,
    tmp_path: Path,
    *,
    bindings: dict[str, int] | None = None,
) -> VideoTemplate:
    """A compiled template whose every PNG exists on disk.

    The files matter: the plan resolves each one to a URL through the project's rows and the
    template's build directory, and an unmapped path raises. Creating them here means the test
    exercises the same code the router runs, rather than a stub of it.
    """
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (1920, 1080), (20, 20, 20, 255)).save(build / "background.png")
    # The bare twin: identical inside the box rect, which is what makes "draw nothing" for a
    # plate gap equivalent to drawing the crop (§19.31.3).
    Image.new("RGBA", (1920, 1080), (20, 20, 20, 255)).save(build / "background_bare.png")
    Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)).save(build / "overlay.png")
    slots = json.loads(json.dumps(SLOTS))
    for index, slot in enumerate(slots["avatars"]):
        lit = build / f"avatar{index}_lit.png"
        dim = build / f"avatar{index}_dim.png"
        Image.new("RGBA", (16, 16), (250, 250, 250, 255)).save(lit)
        Image.new("RGBA", (16, 16), (110, 110, 110, 255)).save(dim)
        slot["lit_path"] = str(lit)
        slot["dim_path"] = str(dim)

    row = VideoTemplate(
        id=new_id("vtpl"),
        project_id=project_id,
        source_path="",
        source_sha256="0" * 64,
        canvas_w=1920,
        canvas_h=1080,
        background_path=str(build / "background.png"),
        overlay_path=str(build / "overlay.png"),
        box_rect=json.dumps({"x": 240, "y": 209, "w": 1440, "h": 810}),
        box_fit_default="contain",
        slots_json=json.dumps(slots, ensure_ascii=False),
        bindings_json=json.dumps(bindings or {}, ensure_ascii=False),
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def make_asset(session: Session, project_id: str, tmp_path: Path, *, kind: str = "image") -> VideoAsset:
    path = tmp_path / f"{new_id('f')}.png"
    Image.new("RGB", (800, 600), (120, 30, 30)).save(path)
    row = VideoAsset(
        id=new_id("vasset"),
        project_id=project_id,
        kind=kind,
        path=str(path),
        sha256=new_id("sha"),
        width=800,
        height=600,
        duration_ms=0,
        has_audio=False,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def make_item(
    session: Session,
    project_id: str,
    *,
    start_ms: int,
    end_ms: int,
    asset_id: str,
    fit: str = "contain",
    placement: str = "box",
) -> VideoItem:
    row = VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind="media",
        start_ms=start_ms,
        end_ms=end_ms,
        z=0,
        fit=fit,
        motion="none",
        asset_id=asset_id,
        deck_id=None,
        slide_id=None,
        gallery_json="[]",
        orphaned=False,
        muted=True,
        solo=False,
        placement=placement,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def with_speaker(session: Session, *, step: int = 500):
    """A version with three sentences, all spoken by speaker ``local_id`` 0.

    ``step`` **must** be non-zero: ``build_edl`` refuses a zero-length audio, and a fixture
    that produced one would fail in the EDL rather than in the thing under test.
    """
    from animatter.ids import new_id
    from animatter.models import Sentence, Speaker

    version = seed_version(session, text="剧集标签很好用", sizes=[3, 3, 3], step=step)
    session.add(
        Speaker(
            id=new_id("spk"),
            project_id=version.project_id,
            local_id=0,
            name="彼方",
            color="",
            created_at=utcnow_iso(),
        )
    )
    session.flush()
    for sentence in session.scalars(
        select(Sentence).where(Sentence.version_id == version.id)
    ).all():
        sentence.speaker_id = 0
    session.flush()
    return version


@pytest.fixture()
def planned(
    session: Session, tmp_path: Path, settings: object
) -> video_plan.VideoPlan:
    """A plan for a project with one box block and one bound avatar slot."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={"头像1": 0})
    asset = make_asset(session, version.project_id, tmp_path)
    make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    return video_plan.build(session, settings, version)


# --- coverage and numbering ---------------------------------------------


def test_segments_are_contiguous_on_the_output_clock(planned: video_plan.VideoPlan) -> None:
    """Half-open ``[start, end)``, ascending, and never shorter than a millisecond."""
    previous = 0
    for segment in planned.segments:
        assert segment.end_ms > segment.start_ms
        assert segment.start_ms >= previous, "segments must not overlap"
        previous = segment.end_ms


def test_a_plate_gap_is_a_hole_and_not_a_segment(planned: video_plan.VideoPlan) -> None:
    """Dropping plate stretches must leave the surrounding segments **numbered in order**.

    The bug this pins: numbering by the box plan's own index jumps by however many plate
    segments were dropped, and every backdrop file name then points one segment off.
    """
    plate_like = [index for index, segment in enumerate(planned.segments) if segment.kind == "plate"]
    assert plate_like == [], "a plate must never reach the browser"
    assert [segment.start_ms for segment in planned.segments] == sorted(
        segment.start_ms for segment in planned.segments
    )
    # Searching the timeline never returns a segment whose span does not contain the instant.
    for at_ms in (0, 700, 1_400):
        found = planned.segment_at(at_ms)
        if found is not None:
            assert found.start_ms <= at_ms < found.end_ms


def test_the_search_is_half_open(planned: video_plan.VideoPlan) -> None:
    """``end_ms`` belongs to the next segment; off by one there freezes the last frame."""
    for segment in planned.segments[:-1]:
        assert planned.segment_at(segment.end_ms) is not segment
        assert planned.segment_at(segment.start_ms) is segment


def test_an_instant_after_the_end_finds_nothing(planned: video_plan.VideoPlan) -> None:
    assert planned.segment_at(planned.output_duration_ms + 1) is None


# --- the layer decisions -------------------------------------------------


def test_an_unbound_slot_gets_no_element(planned: video_plan.VideoPlan) -> None:
    """Only the bound slot has a track, so only it may be drawn.

    Drawing the other slot would show the compiled poster's bright photo — and the export
    deliberately switched to ``background_bare.png`` so that frame is empty (§19.5).
    """
    assert [avatar.name for avatar in planned.avatars] == ["头像1"]


def test_the_base_poster_is_the_bare_one_when_a_track_exists(
    planned: video_plan.VideoPlan,
) -> None:
    assert planned.background_url.endswith("background_bare.png")


def test_the_base_poster_is_the_full_one_without_tracks(
    session: Session, tmp_path: Path, settings: object
) -> None:
    """No binding, no tracks, so the compiled poster already has the right faces."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    asset = make_asset(session, version.project_id, tmp_path)
    make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    plan = video_plan.build(session, settings, version)
    assert plan.avatars == ()
    assert plan.background_url.endswith("/background.png")


def test_lit_avatars_follow_the_intervals(planned: video_plan.VideoPlan) -> None:
    avatar = planned.avatars[0]
    assert avatar.intervals, "the fixture's speaker talks"
    first = avatar.intervals[0]
    assert planned.lit_avatars(first.start_ms) == {"头像1"}
    # 1 ms past the end: the interval is half-open, so the slot is dim.
    assert planned.lit_avatars(first.end_ms) == set()


def test_the_stroke_is_a_separate_layer_from_the_box(planned: video_plan.VideoPlan) -> None:
    """The DOM must draw the stroke under the box content, not merged into the poster.

    Measured on the real template: 5930 semi-opaque stroke pixels fall inside the box rect,
    so merging them into the background would put the stroke *over* the photo and the preview
    would disagree with `build_filtergraph`, which overlays the layers first (§19.31.3).

    This row predates the split, so it has no `art` block: the server reports a full-canvas
    stroke and no plate, which is exactly the shape an uncompiled old project has.
    """
    assert planned.stroke_url is not None
    assert planned.stroke_url.endswith("/overlay.png")
    assert "background" not in planned.stroke_url
    assert planned.stroke_rect == {"x": 0, "y": 0, "w": 1920, "h": 1080}
    # No plate in the split sense: this poster still has it baked in.
    assert planned.plate_url is None
    assert planned.plate_rect is None
    # And the payload carries the same four fields, so the wire format and the plan agree.
    payload = video_plan.payload(planned)
    assert payload["stroke_url"] == planned.stroke_url
    assert payload["plate_url"] is None
    assert "overlay_url" not in payload


def test_a_split_template_publishes_the_plate_and_the_stroke(
    session: Session, tmp_path: Path, settings: object
) -> None:
    """A row with an `art` block reports both pieces at their own rects (§19.35).

    `_url_map` maps *every* file in the build directory, which is why the split needed no new
    route — the assertion is that the plan actually names the two files and their rects, and
    that the plate's rect is not the canvas (a plate stretched to 1920x1080 is a broken frame).
    """
    version = with_speaker(session)
    template = make_template(session, version.project_id, tmp_path, bindings={"头像1": 0})
    build = tmp_path / "build"
    Image.new("RGBA", (1444, 815), (244, 244, 244, 255)).save(build / "plate.png")
    slots = json.loads(template.slots_json)
    slots["art"] = {
        "plate": {"path": "plate.png", "rect": {"x": 238, "y": 206, "w": 1444, "h": 815}},
        "stroke": {"path": "overlay.png", "rect": {"x": 231, "y": 198, "w": 1456, "h": 829}},
    }
    template.slots_json = json.dumps(slots, ensure_ascii=False)
    asset = make_asset(session, version.project_id, tmp_path)
    make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    plan = video_plan.build(session, settings, version)
    assert plan.plate_url is not None and plan.plate_url.endswith("/plate.png")
    assert plan.plate_rect == {"x": 238, "y": 206, "w": 1444, "h": 815}
    assert plan.stroke_url is not None and plan.stroke_url.endswith("/overlay.png")
    assert plan.stroke_rect == {"x": 231, "y": 198, "w": 1456, "h": 829}
    # The crop changed the geometry, so it has to change the cache key too, or moving the
    # frame would keep serving the old still.
    slots["art"]["plate"]["rect"] = {"x": 300, "y": 206, "w": 1444, "h": 815}
    template.slots_json = json.dumps(slots, ensure_ascii=False)
    session.commit()
    moved = video_plan.build(session, settings, version)
    assert moved.digest != plan.digest


# --- cues ----------------------------------------------------------------


def test_a_cue_band_is_anchored_to_the_bottom(planned: video_plan.VideoPlan) -> None:
    assert planned.cues, "the fixture has sentences, so it must have cues"
    for cue in planned.cues:
        assert cue.top + cue.height == planned.canvas_h


def test_a_cue_band_grows_with_the_line_count(planned: video_plan.VideoPlan) -> None:
    """The height is style-only, which is what lets the plan publish it before baking."""
    style = SubtitleStyle()
    short = "短"
    long = "这是一句很长的话，" * 8
    from animatter.media import cue_png

    short_height = cue_png.band_height(short, style, play_w=1920, canvas_h=1080)
    long_height = cue_png.band_height(long, style, play_w=1920, canvas_h=1080)
    assert len(wrap_cjk(long, style=style, play_w=1920)) > len(
        wrap_cjk(short, style=style, play_w=1920)
    )
    assert long_height > short_height


def test_cue_bake_names_are_unique_and_carry_the_digest(planned: video_plan.VideoPlan) -> None:
    names = [cue.url for cue in planned.cues if cue.url]
    assert len(names) == len(set(names))
    for cue in planned.cues:
        assert cue.url is not None
        assert planned.digest in cue.url


@pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg on PATH")
def test_a_baked_band_is_exactly_the_crop_of_the_full_canvas(
    tmp_path: Path, settings: object
) -> None:
    """The whole correctness argument for the band shape (§19.31.4).

    Rendering on a short canvas with ``original_size`` is **not** a crop — libass re-fits
    PlayRes and squashes the text (measured 46 000+ differing pixels). So the band must be a
    crop of a full-canvas render, and this asserts it byte for byte.
    """
    import subprocess

    import numpy as np

    from animatter.media import cue_png
    from animatter.media.subtitle_ass import AssCue, write_ass
    from animatter.media.video import escape_filter_path

    style = SubtitleStyle()
    text = "这句话足够长，会换成两行，因为宽度只够放三十二个字。" * 2
    ffmpeg = settings.ffmpeg_path  # type: ignore[attr-defined]

    band_path = tmp_path / "band.png"
    cue_png.bake(ffmpeg, target=band_path, text=text, style=style)

    full_path = tmp_path / "full.png"
    script = tmp_path / "full.ass"
    write_ass(script, [AssCue(start_ms=0, end_ms=4_000, text=text)], style=style)
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=24:d=1",
            # Escaped, not `as_posix()`: `C:` reads as an option separator inside a filter
            # argument, and ffmpeg then fails with an error that names the filter, not the path.
            "-vf",
            "format=rgba,colorchannelmixer=aa=0,"
            f"ass=filename='{escape_filter_path(script)}':alpha=1",
            "-frames:v", "1", "-update", "1", "-pix_fmt", "rgba", "-c:v", "png",
            str(full_path),
        ],
        check=True,
        capture_output=True,
    )
    band = np.asarray(Image.open(band_path).convert("RGBA"))
    full = np.asarray(Image.open(full_path).convert("RGBA"))
    height = band.shape[0]
    assert np.array_equal(band, full[1080 - height : 1080, :, :])


# --- the payload ---------------------------------------------------------


def test_the_payload_carries_urls_and_no_paths(planned: video_plan.VideoPlan) -> None:
    """A path in the payload would be a file-read primitive waiting to happen (§8)."""
    payload = video_plan.payload(planned)
    blob = json.dumps(payload, ensure_ascii=False)
    assert "C:\\" not in blob and "D:\\" not in blob
    for segment in payload["segments"]:
        assert segment["url"].startswith("/api/"), segment["url"]
        if segment["backdrop_url"]:
            assert segment["backdrop_url"].startswith(video_plan.PLAN_FILE_PREFIX)
    assert payload["background_url"].startswith("/api/")


def test_a_cue_url_carries_the_digest_so_it_can_be_cached_for_ever(
    planned: video_plan.VideoPlan,
) -> None:
    """A URL is only cacheable as `immutable` when a given path can never change meaning.

    Baking is deferred to the route, so the digest has to be *in the path* rather than in a query
    the browser could vary — otherwise the day the plan changes, every cached band is wrong.
    """
    for cue in planned.cues:
        assert planned.digest in cue.url
        assert cue.url.startswith(f"/api/versions/{planned.version_id}/video/cue/")


def test_an_unroutable_file_raises_instead_of_guessing(
    session: Session, tmp_path: Path, settings: object
) -> None:
    """A missing URL mapping is a contract break, not something to paper over.

    The failure mode this prevents is a silent fallback to "serve it as a template asset",
    which would 404 in the browser and look like a rendering bug.
    """
    version = with_speaker(session)
    template = make_template(session, version.project_id, tmp_path, bindings={})
    asset = make_asset(session, version.project_id, tmp_path)
    make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    # A background that is not on disk: the template's own directory is scanned, so a path
    # outside it can never be mapped.
    template.background_path = str(tmp_path / "nowhere" / "background.png")
    session.flush()
    from animatter.errors import AppError

    with pytest.raises(AppError) as caught:
        video_plan.build(session, settings, version)
    assert caught.value.code == "plan_file_unroutable"


# --- the blob variants ---------------------------------------------------


def test_a_blurred_segment_gets_a_backdrop_url(planned: video_plan.VideoPlan) -> None:
    """Only the two blur fits need ffmpeg; everything else is native CSS (§19.31.3)."""
    for segment in planned.segments:
        if segment.fit in video_plan.NEEDS_BACKDROP:
            assert segment.backdrop_url is not None
            assert segment.backdrop_url.startswith(video_plan.PLAN_FILE_PREFIX)
        else:
            assert segment.backdrop_url is None


def test_moving_a_block_changes_the_digest(
    session: Session, tmp_path: Path, settings: object
) -> None:
    """The plan describes the whole film, so an edit anywhere must produce a new digest.

    A still only depends on the frame it draws; a plan that kept its digest after a block moved
    would keep serving the old cue and backdrop names, and the preview would be of a film that
    no longer exists (§19.11 trap 24).
    """
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    asset = make_asset(session, version.project_id, tmp_path)
    item = make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    before = video_plan.build(session, settings, version).digest
    item.end_ms = 900
    session.flush()
    after = video_plan.build(session, settings, version).digest
    assert before != after


@pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg on PATH")
def test_a_looped_clip_publishes_the_stream_period_not_the_container_one(
    tmp_path: Path, settings: object
) -> None:
    """The number a browser must reduce a looped offset by is **ffmpeg's** wrap period.

    Both halves of the preview have to agree to the frame, and the two wrap on different values:
    `-stream_loop -1` repeats the *stream* (here 2.000 s of video) while an `HTMLMediaElement`
    reports the *container* (2.400 s, because the audio track runs longer). Publishing the
    container value — the obvious choice, since it is what `element.duration` returns — makes the
    browser seek 400 ms late on every wrap. On the sample project that was 32–83 ms per loop,
    i.e. four whole frames after three loops, and it is why two instants could never agree
    despite the frame being chosen correctly (§19.29 P1).

    The clip is built with exactly that asymmetry: 2.0 s of video, 2.4 s of audio.
    """
    ffmpeg = settings.ffmpeg_path  # type: ignore[attr-defined]
    clip = tmp_path / "long-audio.mp4"
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=24:d=2",
            "-f", "lavfi", "-i", "sine=f=440:d=2.4",
            "-c:v", "libx264", "-c:a", "aac",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    stream_ms = video_plan.loop_period_ms(settings, clip)  # type: ignore[arg-type]
    container = subprocess.run(
        [
            ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error",
            "-show_entries", "format=duration", "-of", "csv=p=0", str(clip),
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert abs(stream_ms - 2000) <= 45, stream_ms
    assert abs(stream_ms - round(float(container) * 1000)) > 300, (
        "the probe fell back to the container duration, which is the bug this test exists for"
    )
