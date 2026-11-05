"""Waveform peaks and sentence-boundary editing.

The peak cache is tested as a format round-trip and as a windowing contract,
because a mismatch between what the client asks for and what the server returns
shows up as a blank or misaligned waveform — hard to diagnose from the UI.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.media import peaks as peaks_service


def make_peaks(samples: np.ndarray, *, sample_rate: int = 16000) -> peaks_service.Peaks:
    duration_ms = int(len(samples) / sample_rate * 1000)
    levels = []
    for rate in peaks_service.LEVELS:
        per_bucket = max(1, sample_rate // rate)
        levels.append((rate, peaks_service._bucket(samples, per_bucket)))
    return peaks_service.Peaks(duration_ms=duration_ms, sample_rate=sample_rate, levels=tuple(levels))


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


# --- serialization ------------------------------------------------------


def test_round_trip_preserves_data() -> None:
    samples = (np.sin(np.linspace(0, 200, 16000)) * 30000).astype(np.int16)
    original = make_peaks(samples)

    restored = peaks_service.deserialize(peaks_service.serialize(original))

    assert restored.duration_ms == original.duration_ms
    assert len(restored.levels) == len(original.levels)
    for (rate_a, data_a), (rate_b, data_b) in zip(original.levels, restored.levels, strict=True):
        assert rate_a == rate_b
        assert np.array_equal(data_a, data_b)


def test_deserialize_rejects_bad_magic() -> None:
    with pytest.raises(ValueError, match="峰值缓存"):
        peaks_service.deserialize(b"XXXX" + b"\x00" * 20)


def test_serialize_header_is_stable() -> None:
    blob = peaks_service.serialize(make_peaks(np.zeros(1600, dtype=np.int16)))
    assert blob[:4] == b"PKC1"


# --- level selection ----------------------------------------------------


def test_level_for_picks_next_coarser() -> None:
    peaks = make_peaks(np.zeros(16000, dtype=np.int16))
    assert peaks.level_for(1)[0] == 8
    assert peaks.level_for(64)[0] == 64
    assert peaks.level_for(100)[0] == 512


def test_level_for_above_max_uses_finest() -> None:
    peaks = make_peaks(np.zeros(16000, dtype=np.int16))
    assert peaks.level_for(99999)[0] == 4096


# --- windowing ----------------------------------------------------------


def test_window_returns_requested_span() -> None:
    samples = np.ones(16000 * 10, dtype=np.int16) * 1000  # 10 seconds
    peaks = make_peaks(samples)
    payload = peaks_service.window_payload(peaks, buckets_per_second=8, from_ms=2000, to_ms=4000)
    assert payload["rate"] == 8
    # 2 seconds at 8 buckets/s = 16 buckets.
    assert len(payload["min"]) == 16
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
    from pathlib import Path

    path = peaks_service.cache_path(Path("/tmp/proj"), "abc123")
    assert path.name == "abc123.pkc"


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
    assert {t.idx: (t.start_ms, t.end_ms) for t in tokens} == raw_before


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
