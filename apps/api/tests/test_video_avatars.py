"""Avatar slot ↔ speaker binding, and the lit/dim timeline it produces (P3, §19.5).

Two things are checked here that a unit test of the arithmetic cannot see:

* the binding survives a **recompile**, which rewrites ``slots_json`` from the PSD — the
  whole reason it lives in its own column;
* the lit intervals are in **output** time, so a sentence that was deleted does not light
  an avatar up while nobody is speaking.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.edl import Deletion, compile_edl
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.models import (
    Sentence,
    Speaker,
    Token,
    TranscriptVersion,
    VideoTemplate,
)
from animatter.services import video_avatars

#: A template row good enough for the service: it only reads ``slots_json`` and the paths.
#: The PNGs do not have to exist for the *binding* logic, only for a render.
SLOTS = {
    "avatars": [
        {
            "name": "头像1",
            "label": "彼方",
            "rect": {"x": 42, "y": 200, "w": 177, "h": 188},
            "photo": {"x": 15, "y": 30, "w": 203, "h": 202},
            "lit_path": "",
            "dim_path": "",
        },
        {
            "name": "头像2",
            "label": "马小褂",
            "rect": {"x": 42, "y": 417, "w": 177, "h": 187},
            "photo": {"x": 17, "y": 20, "w": 212, "h": 213},
            "lit_path": "",
            "dim_path": "",
        },
    ],
    "texts": [],
    "notes": [],
}


def with_speakers(
    session: Session, *, turns: list[tuple[int, int, int]]
) -> TranscriptVersion:
    """A version whose sentences are ``[(speaker_local_id, start_ms, end_ms), ...]``.

    One token per sentence, which is the smallest shape that still exercises
    ``token_start`` / ``token_end - 1``: a wrong index makes the intervals adjacent instead
    of separate, and that is exactly the kind of error this file exists to catch.
    """
    version = seed_version(session, text="。" * len(turns), step=0)
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
    session.add(
        Speaker(
            id=new_id("spk"),
            project_id=version.project_id,
            local_id=1,
            name="马小褂",
            color="",
            created_at=utcnow_iso(),
        )
    )

    # Replace whatever seed_version created with exactly the spoken turns.
    for token in session.scalars(
        select(Token).where(Token.version_id == version.id)
    ).all():
        session.delete(token)
    for sentence in session.scalars(
        select(Sentence).where(Sentence.version_id == version.id)
    ).all():
        session.delete(sentence)
    session.flush()

    for index, (speaker, start_ms, end_ms) in enumerate(turns):
        session.add(
            Token(
                version_id=version.id,
                idx=index,
                start_ms=start_ms,
                end_ms=end_ms,
                text="字",
                kind="char",
                src_word_idx=index,
                punc_after="",
                derived=True,
                revised=False,
            )
        )
        session.add(
            Sentence(
                id=new_id("sen"),
                version_id=version.id,
                ord=index,
                token_start=index,
                token_end=index + 1,
                speaker_id=speaker,
                origin="asr",
                updated_at=utcnow_iso(),
            )
        )
    session.flush()
    return version


def make_template(
    session: Session, project_id: str, tmp_path: Path
) -> VideoTemplate:
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    slots = json.loads(json.dumps(SLOTS))
    for index, slot in enumerate(slots["avatars"]):
        lit = build / f"avatar{index}_lit.png"
        dim = build / f"avatar{index}_dim.png"
        from PIL import Image

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
        overlay_path="",
        box_rect=json.dumps({"x": 240, "y": 209, "w": 1440, "h": 810}),
        box_fit_default="blur_fill",
        slots_json=json.dumps(slots, ensure_ascii=False),
        bindings_json="{}",
        status="ready",
        error="",
        created_at=utcnow_iso(),
        compiled_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def edl_without_deletions(total_ms: int) -> object:
    """Output time is source time, so an expected interval can be written directly."""
    return compile_edl(
        original_duration_ms=total_ms, deletions=[], snap_to_silences=False, guard_ms=0
    )


def edl_cutting(start_ms: int, end_ms: int, total_ms: int) -> object:
    return compile_edl(
        original_duration_ms=total_ms,
        deletions=[Deletion(start_ms, end_ms)],
        snap_to_silences=False,
        guard_ms=0,
    )


# --- bindings -----------------------------------------------------------


def test_a_binding_is_stored_and_read_back(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)
    session.flush()

    assert video_avatars.bindings_of(row) == {"头像1": 0}


def test_a_binding_survives_a_slots_rewrite(session: Session, tmp_path: Path) -> None:
    """The reason bindings are not inside ``slots_json``: a recompile rewrites that.

    Re-importing the same PSD is ordinary — the user changed a colour in Photoshop. If the
    binding lived in ``slots_json`` it would silently vanish, and the symptom would be
    「头像不跟着说话了」 with nothing in the UI to explain it.
    """
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像2", speaker_id=1)
    session.flush()
    row.slots_json = json.dumps(SLOTS, ensure_ascii=False)

    assert video_avatars.bindings_of(row) == {"头像2": 1}


def test_unbinding_removes_the_key(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)
    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=None)

    assert video_avatars.bindings_of(row) == {}


def test_an_unknown_slot_is_refused(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    make_template(session, version.project_id, tmp_path)

    with pytest.raises(AppError) as caught:
        video_avatars.set_binding(session, version.project_id, slot="不存在", speaker_id=0)
    assert caught.value.status == 404
    assert "头像位" in caught.value.message


def test_an_unknown_speaker_is_refused(session: Session, tmp_path: Path) -> None:
    """A stale id from another project would light the wrong slot with no way to see why."""
    version = with_speakers(session, turns=[(0, 0, 1000)])
    make_template(session, version.project_id, tmp_path)

    with pytest.raises(AppError) as caught:
        video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=99)
    assert "说话人" in caught.value.message


def test_a_malformed_bindings_blob_degrades_to_nothing(
    session: Session, tmp_path: Path
) -> None:
    """Must not break an export after minutes of encoding."""
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)
    row.bindings_json = "{not json"
    assert video_avatars.bindings_of(row) == {}


def test_a_negative_speaker_id_is_ignored(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)
    row.bindings_json = '{"头像1": -1}'
    assert video_avatars.bindings_of(row) == {}


def test_a_non_numeric_speaker_id_is_ignored(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)
    row.bindings_json = '{"头像1": "abc"}'
    assert video_avatars.bindings_of(row) == {}


# --- lit intervals ------------------------------------------------------


def test_a_speakers_sentences_become_lit_intervals(
    session: Session, tmp_path: Path
) -> None:
    version = with_speakers(
        session, turns=[(0, 0, 2000), (1, 2000, 4000), (0, 4000, 5000)]
    )
    intervals = video_avatars.speaking_intervals(
        session, version, edl_without_deletions(5000), 0
    )
    assert [(item.start_ms, item.end_ms) for item in intervals] == [
        (0, 2000),
        (4000, 5000),
    ]


def test_a_breath_shorter_than_the_bridge_keeps_the_avatar_lit(
    session: Session, tmp_path: Path
) -> None:
    """400 ms between two sentences of one speaker: no flicker."""
    version = with_speakers(session, turns=[(0, 0, 1000), (0, 1400, 2000)])
    intervals = video_avatars.speaking_intervals(
        session, version, edl_without_deletions(2000), 0
    )
    assert [(item.start_ms, item.end_ms) for item in intervals] == [(0, 2000)]


def test_a_long_gap_splits_the_avatar_into_two_turns(
    session: Session, tmp_path: Path
) -> None:
    version = with_speakers(session, turns=[(0, 0, 1000), (0, 3000, 4000)])
    intervals = video_avatars.speaking_intervals(
        session, version, edl_without_deletions(4000), 0
    )
    assert [(item.start_ms, item.end_ms) for item in intervals] == [
        (0, 1000),
        (3000, 4000),
    ]


def test_an_unbound_slot_is_absent_from_the_resolution(
    session: Session, tmp_path: Path
) -> None:
    """Absent, not empty: the renderer leaves the compiled background alone for it."""
    version = with_speakers(session, turns=[(0, 0, 2000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)

    resolved = video_avatars.resolve(
        session, row, version, edl_without_deletions(2000)
    )
    assert list(resolved) == ["头像1"]


def test_a_speaker_who_never_talks_is_absent(session: Session, tmp_path: Path) -> None:
    """Bound to speaker 1, but only speaker 0 speaks: nothing to animate."""
    version = with_speakers(session, turns=[(0, 0, 2000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像2", speaker_id=1)

    assert video_avatars.resolve(session, row, version, edl_without_deletions(2000)) == {}


def test_describe_reports_talk_time_per_slot(session: Session, tmp_path: Path) -> None:
    version = with_speakers(session, turns=[(0, 0, 3000), (1, 3000, 5000)])
    row = make_template(session, version.project_id, tmp_path)

    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)
    video_avatars.set_binding(session, version.project_id, slot="头像2", speaker_id=1)

    described = {
        item["name"]: item
        for item in video_avatars.describe(
            session, row, version, edl_without_deletions(5000)
        )
    }
    assert described["头像1"]["lit_ms"] == 3000
    assert described["头像2"]["lit_ms"] == 2000
    assert described["头像1"]["speaker_name"] == "彼方"
    assert described["头像1"]["share"] == pytest.approx(0.6)
    assert described["头像1"]["x"] == 42


def test_describe_carries_the_slot_geometry(session: Session, tmp_path: Path) -> None:
    """The renderer places the track from these numbers; a silent zero would be invisible."""
    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)

    described = {
        item["name"]: item
        for item in video_avatars.describe(
            session, row, version, edl_without_deletions(1000)
        )
    }
    assert (described["头像1"]["x"], described["头像1"]["y"]) == (42, 200)
    assert (described["头像1"]["name"], described["头像1"]["label"]) == ("头像1", "彼方")


def test_avatar_tracks_carry_the_slot_rect_not_the_png_size(
    session: Session, tmp_path: Path
) -> None:
    """Placement size, not artwork size — measured in pixels, and it bit the artefact.

    The compiled PNGs are the PSD group's own size (236×249 on the sample) and the slot
    rect is 177×188. Overlaying at the *artwork* size makes each avatar 32 px taller than
    the gap between slots (the two frames touch) and 47 px wider (it runs into the media
    box), and the upper avatar's opaque plate covers the lower one's name plate.
    """
    from animatter.services import video_render

    version = with_speakers(session, turns=[(0, 0, 1000)])
    row = make_template(session, version.project_id, tmp_path)
    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)

    tracks = video_render.avatar_tracks(
        session, row, version, edl_without_deletions(1000)
    )
    assert len(tracks) == 1
    track = tracks[0]
    assert (track.x, track.y) == (42, 200)
    assert (track.width, track.height) == (177, 188), "must be the slot rect, not 236×249"


def test_avatar_overlays_do_not_overlap_or_reach_the_box(
    session: Session, tmp_path: Path
) -> None:
    """Two slots on the same side must stay inside the margin between them.

    This is the property the user saw fail: the frames touched, and both ran into the media
    box. Checked as arithmetic on the geometry rather than on pixels, so it fails at the
    source instead of after a 14-second encode.
    """
    from animatter.services import video_render

    version = with_speakers(session, turns=[(0, 0, 1000), (1, 500, 1500)])
    row = make_template(session, version.project_id, tmp_path)
    video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)
    video_avatars.set_binding(session, version.project_id, slot="头像2", speaker_id=1)

    tracks = video_render.avatar_tracks(
        session, row, version, edl_without_deletions(2000)
    )
    assert len(tracks) == 2
    ordered = sorted(tracks, key=lambda item: item.y)
    upper, lower = ordered[0], ordered[1]
    assert upper.y + upper.height <= lower.y, "the two avatar frames must not overlap"

    box_left = json.loads(row.box_rect)["x"]
    for track in tracks:
        assert track.x + track.width <= box_left, (
            f"{track.name} reaches x={track.x + track.width}, past the box edge {box_left}"
        )


# --- output time --------------------------------------------------------


def test_a_deleted_sentence_does_not_light_the_avatar(
    session: Session, tmp_path: Path
) -> None:
    """The whole point of mapping through the EDL.

    Sentence 1 (speaker 0, 3000–4000) is deleted. Mapping the raw times would light the
    avatar for a second of silence at 3000 ms of output time.
    """
    version = with_speakers(
        session, turns=[(0, 0, 2000), (0, 3000, 4000), (1, 4000, 6000)]
    )
    intervals = video_avatars.speaking_intervals(
        session, version, edl_cutting(3000, 4000, total_ms=6000), 0
    )
    assert [(item.start_ms, item.end_ms) for item in intervals] == [(0, 2000)]


def test_a_partially_deleted_sentence_is_clipped_not_dropped(
    session: Session, tmp_path: Path
) -> None:
    version = with_speakers(session, turns=[(0, 0, 4000)])
    intervals = video_avatars.speaking_intervals(
        session, version, edl_cutting(1000, 3000, total_ms=4000), 0
    )
    # 0–1000 and 3000–4000 survive; in output time that is one 0–2000 stretch.
    assert [(item.start_ms, item.end_ms) for item in intervals] == [(0, 2000)]


def test_intervals_never_exceed_the_film(session: Session, tmp_path: Path) -> None:
    """A track longer than the film pushes the muxed duration past the audio."""
    version = with_speakers(session, turns=[(0, 0, 4000)])
    intervals = video_avatars.speaking_intervals(
        session, version, edl_cutting(0, 2000, total_ms=4000), 0
    )
    edl = edl_cutting(0, 2000, total_ms=4000)
    for item in intervals:
        assert 0 <= item.start_ms < item.end_ms <= edl.output_duration_ms


def test_a_project_without_a_template_is_refused(session: Session) -> None:
    version = seed_version(session, text="没有模板", step=100)
    with pytest.raises(AppError):
        video_avatars.set_binding(session, version.project_id, slot="头像1", speaker_id=0)
