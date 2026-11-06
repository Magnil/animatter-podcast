"""Waveform peaks and sentence-boundary editing.

The peak cache is tested as a format round-trip and as a windowing contract,
because a mismatch between what the client asks for and what the server returns
shows up as a blank or misaligned waveform — hard to diagnose from the UI.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.media import peaks as peaks_service


def make_peaks(samples: np.ndarray, *, sample_rate: int = 16000) -> peaks_service.Peaks:
    duration_ms = int(len(samples) / sample_rate * 1000)
    buckets = min(peaks_service.MAX_BUCKETS, len(samples))
    per_bucket = max(1, len(samples) // max(1, buckets))
    pairs = peaks_service._bucket(samples, per_bucket)
    return peaks_service.Peaks(
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        min=peaks_service._quantise(pairs[:, 0]),
        max=peaks_service._quantise(pairs[:, 1]),
    )


# --- bucketing ----------------------------------------------------------


def test_bucket_produces_min_and_max() -> None:
    samples = np.array([0, 100, -200, 300, 0, 0], dtype=np.int16)
    buckets = peaks_service._bucket(samples, 3)
    assert buckets.shape == (2, 2)
    assert buckets[0].tolist() == [-200, 100]
    assert buckets[1].tolist() == [0, 300]


def test_bucket_drops_partial_tail() -> None:
    samples = np.array([1, 2, 3, 4, 5], dtype=np.int16)
    assert peaks_service._bucket(samples, 2).shape == (2, 2)


def test_bucket_empty_input() -> None:
    assert peaks_service._bucket(np.array([], dtype=np.int16), 4).shape == (0, 2)


def test_bucket_zero_size_clamped() -> None:
    samples = np.array([1, 2, 3], dtype=np.int16)
    assert peaks_service._bucket(samples, 0).shape == (3, 2)


# --- quantisation -------------------------------------------------------


def test_quantise_keeps_full_scale_inside_int8() -> None:
    # A hot source overshoots ±32767; clamping rather than wrapping is what
    # keeps the drawn shape from inverting at the loudest moments.
    values = np.array([-32768, -16384, 0, 16384, 32767, 40000], dtype=np.int32)
    got = peaks_service._quantise(values).tolist()
    assert got[0] == -127
    assert got[2] == 0
    assert got[4] == 127
    assert got[5] == 127, "overshoot must clamp, not wrap"


def test_quantise_is_symmetric() -> None:
    values = np.array([-16384, 16384], dtype=np.int32)
    lo, hi = peaks_service._quantise(values).tolist()
    assert lo == -hi


# --- serialization ------------------------------------------------------


def test_round_trip_preserves_data() -> None:
    samples = (np.sin(np.linspace(0, 200, 16000)) * 30000).astype(np.int16)
    original = make_peaks(samples)

    restored = peaks_service.deserialize(peaks_service.serialize(original))

    assert restored.duration_ms == original.duration_ms
    assert restored.count == original.count
    assert np.array_equal(restored.min, original.min)
    assert np.array_equal(restored.max, original.max)


def test_deserialize_rejects_bad_magic() -> None:
    with pytest.raises(ValueError, match="峰值缓存"):
        peaks_service.deserialize(b"XXXX" + bytes(20))


def test_deserialize_rejects_truncated_blob() -> None:
    # A half-written 4 MB download must fail loudly rather than draw a
    # half-length waveform that silently misaligns every word against the audio.
    blob = peaks_service.serialize(make_peaks(np.zeros(1600, dtype=np.int16)))
    with pytest.raises(ValueError, match="截断"):
        peaks_service.deserialize(blob[:-1])


def test_serialize_header_is_stable() -> None:
    blob = peaks_service.serialize(make_peaks(np.zeros(1600, dtype=np.int16)))
    assert blob[:4] == b"PKE1"


def test_envelope_size_is_two_bytes_per_bucket() -> None:
    # The whole design rests on this: 2M buckets must be ~4 MB, not ~16 MB.
    peaks = make_peaks(np.zeros(16000 * 60, dtype=np.int16))
    blob = peaks_service.serialize(peaks)
    assert len(blob) == 18 + peaks.count * 2


# --- bucket geometry ----------------------------------------------------


def test_bucket_ms_follows_file_length() -> None:
    peaks = make_peaks(np.zeros(16000 * 10, dtype=np.int16))
    assert peaks.bucket_ms == pytest.approx(1000 / 16000, rel=0.01)


def test_empty_peaks_report_zero_bucket_ms() -> None:
    empty = np.array([], dtype=np.int8)
    peaks = peaks_service.Peaks(duration_ms=0, sample_rate=16000, min=empty, max=empty)
    assert peaks.bucket_ms == 0.0
    assert peaks.count == 0


# --- windowing (kept for scripting; the editor no longer calls it) ------


def test_window_returns_requested_span() -> None:
    samples = np.ones(16000 * 10, dtype=np.int16) * 1000  # 10 seconds
    peaks = make_peaks(samples)
    payload = peaks_service.window_payload(peaks, buckets_per_second=8, from_ms=2000, to_ms=4000)
    # 2 seconds at 8 buckets/s ≈ 16 buckets (exact count depends on decimation).
    assert 8 <= len(payload["min"]) <= 20
    assert payload["from_ms"] == pytest.approx(2000, abs=200)


def test_window_normalises_to_unit_range() -> None:
    samples = np.array([-32768, 32767] * 8000, dtype=np.int16)
    peaks = make_peaks(samples)
    payload = peaks_service.window_payload(peaks, buckets_per_second=64, from_ms=0, to_ms=1000)
    assert min(payload["min"]) >= -1.0
    assert max(payload["max"]) <= 1.0


def test_window_clamps_to_duration() -> None:
    peaks = make_peaks(np.zeros(16000, dtype=np.int16))
    payload = peaks_service.window_payload(peaks, buckets_per_second=64, from_ms=500, to_ms=999_999)
    assert payload["to_ms"] <= peaks.duration_ms + 10


def test_window_past_end_returns_empty_arrays() -> None:
    peaks = make_peaks(np.zeros(16000, dtype=np.int16))
    payload = peaks_service.window_payload(peaks, buckets_per_second=64, from_ms=10_000, to_ms=20_000)
    assert payload["min"] == [] or len(payload["min"]) <= 1


def test_cache_path_uses_sha_not_filename() -> None:
    from pathlib import Path as _Path

    path = peaks_service.cache_path(_Path("/tmp/proj"), "abc123")
    assert path.name == "abc123.pke"


def test_legacy_cache_is_removed(tmp_path: Path) -> None:
    """The old 143 MB multi-level file must not linger beside the new one."""
    legacy = tmp_path / "peaks" / "sha-old.pkc"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"PKC1" + bytes(64))

    peaks_service.forget_legacy_cache(tmp_path, "sha-old")
    assert not legacy.exists()
    # Idempotent: a second call must not raise.
    peaks_service.forget_legacy_cache(tmp_path, "sha-old")


def test_memory_cache_avoids_a_second_disk_read(tmp_path: Path) -> None:
    """The 99 ms re-deserialise per request is what this cache exists to remove."""
    from pathlib import Path as _Path

    samples = (np.sin(np.linspace(0, 400, 16000 * 2)) * 20000).astype(np.int16)
    built = make_peaks(samples)
    target = peaks_service.cache_path(tmp_path, "sha-mem")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(peaks_service.serialize(built))
    peaks_service.forget("sha-mem")

    first = peaks_service.load_or_build("ffmpeg", tmp_path, _Path("unused.wav"), "sha-mem")
    target.unlink()  # prove the second call never touches disk
    second = peaks_service.load_or_build("ffmpeg", tmp_path, _Path("unused.wav"), "sha-mem")
    assert second is first


# --- boundary editing ---------------------------------------------------


from tests.test_corrections import seed_version  # noqa: E402  (shared fixture helper)


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def test_new_sentence_splits_selection_out(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三四五六七八", sizes=[8])
    spans = editing.apply_boundary(session, version.id, action="new_sentence", from_token=2, to_token=5)
    assert [(s.token_start, s.token_end) for s in spans] == [(0, 2), (2, 5), (5, 8)]


def test_new_sentence_at_existing_boundary(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    spans = editing.apply_boundary(session, version.id, action="new_sentence", from_token=3, to_token=5)
    assert [(s.token_start, s.token_end) for s in spans] == [(0, 3), (3, 5), (5, 6)]


def test_merge_collapses_selection(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三四五六七八", sizes=[2, 2, 2, 2])
    spans = editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert [(s.token_start, s.token_end) for s in spans] == [(0, 6), (6, 8)]


def test_to_prev_moves_head_up(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    spans = editing.apply_boundary(session, version.id, action="to_prev", from_token=3, to_token=4)
    assert [(s.token_start, s.token_end) for s in spans] == [(0, 4), (4, 6)]


def test_invalid_boundary_has_hint(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三", sizes=[3])
    with pytest.raises(editing.EditError) as caught:
        editing.apply_boundary(session, version.id, action="to_prev", from_token=0, to_token=1)
    assert caught.value.hint


def test_boundary_edit_recorded_in_history(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二三四", sizes=[4])
    editing.apply_boundary(session, version.id, action="new_sentence", from_token=0, to_token=2)
    ops = editing.list_ops(session, version.id)
    assert any(op.op_type == "boundary" for op in ops)


def test_undo_restores_previous_boundaries(session: Session) -> None:
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert len(transcribe_service.load_sentences(session, version.id)) == 1

    editing.undo_last(session, version.id)
    assert len(transcribe_service.load_sentences(session, version.id)) == 2


def test_undo_without_history_raises(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="一二", sizes=[2])
    with pytest.raises(editing.EditError, match="没有可撤销"):
        editing.undo_last(session, version.id)


def test_text_edit_preserves_timeline(session: Session) -> None:
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="剧集标签很好用", sizes=[7])
    raw_before = {t.idx: (t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version.id)}

    editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集", proposed="句级")
    tokens = transcribe_service.load_tokens(session, version.id)
    assert "".join(t.text for t in tokens) == "句级标签很好用"

    # The edited window keeps both of its outer edges: 0ms in, 200ms out. Word
    # level means two source tokens collapse into one, so comparing whole index
    # maps would be wrong — what must hold is that no time moved.
    assert (tokens[0].start_ms, tokens[0].end_ms) == (0, 200)
    assert raw_before[0][0] == tokens[0].start_ms
    assert raw_before[1][1] == tokens[0].end_ms

    # Everything after the window keeps its exact timing.
    after_before = {i: raw_before[i] for i in range(2, len(raw_before))}
    after_now = {i + 1: (t.start_ms, t.end_ms) for i, t in enumerate(tokens[1:])}
    assert list(after_now.values()) == list(after_before.values())


def test_text_edit_missing_text_has_current_hint(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="句级标签", sizes=[4])
    with pytest.raises(editing.EditError) as caught:
        editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集", proposed="句级")
    assert "当前句子" in caught.value.hint


def test_text_edit_recorded_separately_from_boundary(session: Session) -> None:
    from animatter.services import editing

    version = seed_version(session, text="剧集标签", sizes=[4])
    editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集", proposed="句级")
    ops = editing.list_ops(session, version.id)
    assert [op.op_type for op in ops] == ["text"]


# --- the revised flag (what the editor paints blue) ---------------------


def test_text_edit_marks_the_rewritten_word_as_revised(session: Session) -> None:
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="剧集标签很好用", sizes=[7])
    editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集", proposed="句级")

    tokens = transcribe_service.load_tokens(session, version.id)
    assert tokens[0].text == "句级"
    assert tokens[0].revised is True
    # Nothing else was touched, so nothing else turns blue.
    assert [t.revised for t in tokens[1:]] == [False] * (len(tokens) - 1)


def test_freshly_transcribed_words_are_not_revised(session: Session) -> None:
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="剧集标签", sizes=[4])
    assert [t.revised for t in transcribe_service.load_tokens(session, version.id)] == [False] * 4


def test_the_revised_flag_reaches_the_transcript_endpoint(
    client: TestClient, session: Session
) -> None:
    """The editor colours by this field, so it must survive serialization."""
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="剧集标签很好用", sizes=[7])
    session.commit()
    editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集", proposed="句级")
    session.commit()

    payload = client.get(f"/api/versions/{version.id}/transcript").json()
    assert payload["tokens"][0]["revised"] is True
    assert payload["tokens"][0]["text"] == "句级"
    assert payload["tokens"][1]["revised"] is False
    assert len(transcribe_service.load_tokens(session, version.id)) == len(payload["tokens"])


# --- denormalised counters ----------------------------------------------


def test_boundary_edit_updates_the_sentence_count(session: Session) -> None:
    """Regression: the header claimed "3 句" for a 5-sentence transcript.

    ``sentence_count`` is read by the header, the archive and the subtitle panel,
    and a split/merge changes it without touching a single token.
    """
    from animatter.models import TranscriptVersion
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="一二三四五六", sizes=[6])
    assert session.get(TranscriptVersion, version.id).sentence_count == 1

    editing.apply_boundary(session, version.id, action="new_sentence", from_token=2, to_token=4)
    assert session.get(TranscriptVersion, version.id).sentence_count == 3
    assert len(transcribe_service.load_sentences(session, version.id)) == 3

    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert session.get(TranscriptVersion, version.id).sentence_count == 1


def test_text_edit_that_changes_token_count_updates_the_version(session: Session) -> None:
    from animatter.models import TranscriptVersion
    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    # Word-level: "剧集"→"句级" is 1 token → 1 token, so use a split replacement
    # that legitimately changes the count.
    version = seed_version(session, text="剧集标签", sizes=[4])
    editing.apply_text_edit(session, version.id, sentence_ord=0, original="剧集标签", proposed="句级 标签")
    tokens = transcribe_service.load_tokens(session, version.id)

    stored = session.get(TranscriptVersion, version.id)
    assert stored.token_count == len(tokens)
