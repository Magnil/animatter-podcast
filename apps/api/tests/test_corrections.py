"""Applying and rejecting corrections against a real database.

The central assertion of this file: **accepting a correction never moves the
timeline**. Everything downstream (subtitles, cuts, the render EDL) assumes that,
and a regression here corrupts output silently.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.ids import new_id, utcnow_iso
from animatter.models import CorrectionCandidate, Media, Project, Sentence, Token, TranscriptVersion
from animatter.services import corrections as correction_service
from animatter.services import transcribe as transcribe_service


def seed_version(
    session: Session,
    *,
    text: str = "剧集标签很好用",
    sizes: list[int] | None = None,
    step: int = 100,
) -> TranscriptVersion:
    """Create a project, media, and a transcript version with known tokens."""
    project = Project(id=new_id("prj"), name="测试项目", created_at=utcnow_iso(), updated_at=utcnow_iso())
    session.add(project)
    # Flush explicitly: SQLite enforces the FK, and without this the unit of
    # work is free to emit the media INSERT first.
    session.flush()

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path="x.wav",
        sha256=new_id("sha"),
        duration_ms=len(text) * step,
        sample_rate=16000,
        channels=1,
        codec="mp3",
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()

    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id,
        job_id="job",
        version=1,
        created_at=utcnow_iso(),
        duration_ms=len(text) * step,
        token_count=len(text),
        sentence_count=len(sizes) if sizes else 1,
        is_active=True,
    )
    session.add(version)
    session.flush()

    for idx, ch in enumerate(text):
        session.add(
            Token(
                version_id=version.id,
                idx=idx,
                start_ms=idx * step,
                end_ms=(idx + 1) * step,
                text=ch,
                kind="char",
                src_word_idx=0,
                punc_after="",
                derived=True,
            )
        )

    sizes = sizes or [len(text)]
    cursor = 0
    for ord_, size in enumerate(sizes):
        session.add(
            Sentence(
                id=new_id("sen"),
                version_id=version.id,
                ord=ord_,
                token_start=cursor,
                token_end=cursor + size,
                speaker_id=None,
                origin="asr",
                updated_at=utcnow_iso(),
            )
        )
        cursor += size
    session.flush()
    return version


def add_candidate(
    session: Session,
    version: TranscriptVersion,
    *,
    start: int,
    end: int,
    original: str,
    proposed: str,
    hotword: str = "句级标签",
) -> CorrectionCandidate:
    row = CorrectionCandidate(
        id=new_id("cor"),
        version_id=version.id,
        sentence_ord=0,
        token_start=start,
        token_end=end,
        original_text=original,
        proposed_text=proposed,
        matched_hotword=hotword,
        confidence=0.9,
        reason="test",
        status="pending",
        source="llm",
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


# --- happy path ---------------------------------------------------------


def test_apply_same_length_keeps_timeline(session: Session) -> None:
    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")

    correction_service.apply_candidate(session, candidate.id)

    tokens = transcribe_service.load_tokens(session, version.id)
    assert "".join(t.text for t in tokens) == "句级标签很好用"
    assert [(t.start_ms, t.end_ms) for t in tokens] == [(i * 100, (i + 1) * 100) for i in range(7)]


def test_apply_shrink_adjusts_sentence_ranges(session: Session) -> None:
    version = seed_version(session, text="人工智能很好用", sizes=[4, 3])
    candidate = add_candidate(
        session,
        version,
        start=0,
        end=4,
        original="人工智能",
        proposed="AI",
        hotword="AI",
    )

    correction_service.apply_candidate(session, candidate.id)

    tokens = transcribe_service.load_tokens(session, version.id)
    sentences = transcribe_service.load_sentences(session, version.id)

    assert "".join(t.text for t in tokens) == "AI很好用"
    # 7 chars, 4 replaced by a single "AI" token → 4 tokens remain.
    assert len(tokens) == 4
    assert [t.text for t in tokens] == ["AI", "很", "好", "用"]
    # Sentences must still tile the stream exactly (INV-1/INV-2).
    assert sentences[0].token_start == 0
    assert sentences[-1].token_end == len(tokens)
    for previous, current in pairwise(sentences):
        assert previous.token_end == current.token_start


def test_apply_shrink_does_not_move_audio_timestamps(session: Session) -> None:
    version = seed_version(session, text="人工智能很好用", sizes=[4, 3])
    before = transcribe_service.load_tokens(session, version.id)
    candidate = add_candidate(
        session, version, start=0, end=4, original="人工智能", proposed="AI", hotword="AI"
    )

    correction_service.apply_candidate(session, candidate.id)

    after = transcribe_service.load_tokens(session, version.id)
    # The rewritten window must still end where the original did.
    assert after[0].start_ms == before[0].start_ms
    assert after[0].end_ms == before[3].end_ms
    # Everything after the window is untouched.
    assert after[1].start_ms == before[4].start_ms
    assert after[-1].end_ms == before[-1].end_ms


def test_apply_records_edit_op(session: Session) -> None:
    from animatter.models import EditOp

    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    correction_service.apply_candidate(session, candidate.id)

    ops = session.scalars(select(EditOp).where(EditOp.version_id == version.id)).all()
    assert len(ops) == 1
    # Text repair and boundary repair must stay distinguishable.
    assert ops[0].op_type == "retoken_text"


def test_applied_candidate_status(session: Session) -> None:
    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    result = correction_service.apply_candidate(session, candidate.id)
    assert result.status == "accepted"
    assert result.resolved_at is not None


# --- safety rails -------------------------------------------------------


def test_stale_candidate_rejected_when_text_changed(session: Session) -> None:
    version = seed_version(session, text="句级标签很好用")
    # Candidate claims the text is 剧集 but it is already 句级.
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")

    with pytest.raises(correction_service.CorrectionError) as caught:
        correction_service.apply_candidate(session, candidate.id)

    assert caught.value.code == "candidate_stale"
    refreshed = session.get(CorrectionCandidate, candidate.id)
    assert refreshed is not None
    assert refreshed.status == "stale"


def test_double_apply_rejected(session: Session) -> None:
    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    correction_service.apply_candidate(session, candidate.id)

    with pytest.raises(correction_service.CorrectionError) as caught:
        correction_service.apply_candidate(session, candidate.id)
    assert caught.value.code == "candidate_not_pending"


def test_missing_candidate_rejected(session: Session) -> None:
    with pytest.raises(correction_service.CorrectionError, match="候选不存在"):
        correction_service.apply_candidate(session, "cor_missing")


def test_overlapping_candidates_marked_stale(session: Session) -> None:
    version = seed_version(session)
    first = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    second = add_candidate(session, version, start=1, end=3, original="集标", proposed="级标")

    correction_service.apply_candidate(session, first.id)

    refreshed = session.get(CorrectionCandidate, second.id)
    assert refreshed is not None
    # Its span just shifted, so applying it now could rewrite the wrong place.
    assert refreshed.status == "stale"


def test_reject_sets_status(session: Session) -> None:
    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    result = correction_service.reject_candidate(session, candidate.id)
    assert result.status == "rejected"


def test_reject_twice_rejected(session: Session) -> None:
    version = seed_version(session)
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    correction_service.reject_candidate(session, candidate.id)
    with pytest.raises(correction_service.CorrectionError):
        correction_service.reject_candidate(session, candidate.id)


def test_stale_candidate_does_not_modify_tokens(session: Session) -> None:
    version = seed_version(session, text="句级标签很好用")
    candidate = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    with pytest.raises(correction_service.CorrectionError):
        correction_service.apply_candidate(session, candidate.id)

    tokens = transcribe_service.load_tokens(session, version.id)
    assert "".join(t.text for t in tokens) == "句级标签很好用"


# --- listing ------------------------------------------------------------


def test_list_pending_only_by_default(session: Session) -> None:
    version = seed_version(session)
    a = add_candidate(session, version, start=0, end=2, original="剧集", proposed="句级")
    correction_service.reject_candidate(session, a.id)

    assert correction_service.list_candidates(session, version.id) == []
    assert len(correction_service.list_candidates(session, version.id, status=None)) == 1


def test_persist_proposals_deduplicates(session: Session) -> None:
    from animatter.services.proofread import Proposal

    version = seed_version(session)
    proposal = Proposal(
        sentence_ord=0,
        original="剧集",
        proposed="句级",
        matched_hotword="句级标签",
        confidence=0.9,
        reason="",
        token_start=0,
        token_end=2,
    )
    first = correction_service.persist_proposals(session, version.id, [proposal])
    second = correction_service.persist_proposals(session, version.id, [proposal])
    assert len(first) == 1
    assert second == []
