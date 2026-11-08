"""Accepting corrections: rewrite tokens without moving the timeline.

The one invariant that matters here (AGENTS.md §6.5): applying a correction
changes text inside a token range, never the range's time boundaries. Sentence
ranges are adjusted only when the token *count* changes, so a 4-char → 1-token
fix keeps every later subtitle exactly where it was.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.core.realign import (
    RealignError,
    TokenRange,
    apply_replacement,
    shift_ranges_after,
)
from animatter.core.tokens import TokenSpan
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import CorrectionCandidate, EditOp, Sentence, Token
from animatter.services.proofread import Proposal

log = get_logger(__name__)


class CorrectionError(AppError):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(code, message, hint=hint)


def _load_tokens(session: Session, version_id: str) -> list[Token]:
    return list(session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx)))


def _load_sentence_ranges(session: Session, version_id: str) -> list[tuple[int, int]]:
    rows = session.scalars(
        select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord)
    ).all()
    return [(row.token_start, row.token_end) for row in rows]


def list_candidates(
    session: Session, version_id: str, *, status: str | None = "pending"
) -> list[CorrectionCandidate]:
    stmt = select(CorrectionCandidate).where(CorrectionCandidate.version_id == version_id)
    if status is not None:
        stmt = stmt.where(CorrectionCandidate.status == status)
    return list(session.scalars(stmt.order_by(CorrectionCandidate.token_start, CorrectionCandidate.id)))


def apply_candidate(session: Session, candidate_id: str) -> CorrectionCandidate:
    """Rewrite the token range described by one candidate.

    The candidate must still match the current text; if a previous edit changed
    those tokens, the candidate is marked ``stale`` rather than applied to the
    wrong span.
    """
    candidate = session.get(CorrectionCandidate, candidate_id)
    if candidate is None:
        raise CorrectionError("candidate_not_found", "候选不存在", "刷新校对列表。")
    if candidate.status != "pending":
        raise CorrectionError(
            "candidate_not_pending",
            f"候选状态为 {candidate.status}，不能再次应用",
            "只处理 pending 状态的候选。",
        )

    token_rows = _load_tokens(session, candidate.version_id)
    tokens = [_to_span(row) for row in token_rows]

    span = TokenRange(candidate.token_start, candidate.token_end)
    if span.end > len(tokens):
        candidate.status = "stale"
        candidate.resolved_at = utcnow_iso()
        raise CorrectionError("candidate_stale", "候选区间已超出当前 token 总数", "重新跑一遍校对。")

    current_text = "".join(t.text for t in tokens[span.start : span.end])
    if current_text != candidate.original_text:
        candidate.status = "stale"
        candidate.resolved_at = utcnow_iso()
        raise CorrectionError(
            "candidate_stale",
            f"原文已变化（当前为「{current_text}」）",
            "该位置已被其他编辑改过，重新跑校对。",
        )

    try:
        result = apply_replacement(tokens, span, candidate.proposed_text)
    except RealignError as exc:
        raise CorrectionError("realign_failed", str(exc), "该区间无法在不动时间轴的前提下改写。") from exc

    sentence_ranges = _load_sentence_ranges(session, candidate.version_id)
    if result.token_delta != 0:
        sentence_ranges = shift_ranges_after(sentence_ranges, at_token=span.start, delta=result.token_delta)

    # Rewrite the range wholesale: delete then re-insert keeps idx dense without
    # a bulk UPDATE per row (SQLite has no cheap positional update).
    session.query(Token).filter(
        Token.version_id == candidate.version_id,
        Token.idx >= span.start,
        Token.idx < span.end,
    ).delete(synchronize_session=False)

    # Shift the tail indices before inserting the new rows.
    if result.token_delta != 0:
        tail = token_rows[span.end :]
        for row in tail:
            row.idx += result.token_delta
        session.flush()

    for token in result.tokens[span.start : span.start + (span.length + result.token_delta)]:
        session.add(
            Token(
                version_id=candidate.version_id,
                idx=token.idx,
                start_ms=token.start_ms,
                end_ms=token.end_ms,
                text=token.text,
                kind=token.kind,
                src_word_idx=token.src_word_idx,
                punc_after=token.punc_after,
                derived=token.derived,
                revised=token.revised,
            )
        )
    session.flush()

    if result.token_delta != 0:
        _rewrite_sentence_ranges(session, candidate.version_id, sentence_ranges)

    candidate.status = "accepted"
    candidate.resolved_at = utcnow_iso()

    session.add(
        EditOp(
            id=new_id("edt"),
            project_id=_project_of_version(session, candidate.version_id),
            version_id=candidate.version_id,
            seq=_next_seq(session, candidate.version_id),
            op_type="retoken_text",
            # Text repair and boundary repair are recorded separately: mixing
            # them would make "which kind of error is most common" unanswerable.
            payload_json=json.dumps(
                {
                    "candidate_id": candidate.id,
                    "token_start": span.start,
                    "token_end": span.start + span.length + result.token_delta,
                    "original": candidate.original_text,
                    "proposed": candidate.proposed_text,
                    "matched_hotword": candidate.matched_hotword,
                },
                ensure_ascii=False,
            ),
            created_at=utcnow_iso(),
        )
    )

    _invalidate_siblings(session, candidate)
    session.flush()
    log.info("correction.applied", candidate=candidate.id, delta=result.token_delta)
    return candidate


def reject_candidate(session: Session, candidate_id: str) -> CorrectionCandidate:
    candidate = session.get(CorrectionCandidate, candidate_id)
    if candidate is None:
        raise CorrectionError("candidate_not_found", "候选不存在", "刷新校对列表。")
    if candidate.status != "pending":
        raise CorrectionError("candidate_not_pending", f"候选状态为 {candidate.status}")
    candidate.status = "rejected"
    candidate.resolved_at = utcnow_iso()
    session.flush()
    return candidate


def _invalidate_siblings(session: Session, applied: CorrectionCandidate) -> None:
    """Mark overlapping pending candidates stale: their spans just moved."""
    overlapping = session.scalars(
        select(CorrectionCandidate).where(
            CorrectionCandidate.version_id == applied.version_id,
            CorrectionCandidate.status == "pending",
            CorrectionCandidate.id != applied.id,
            CorrectionCandidate.token_start < applied.token_end,
            CorrectionCandidate.token_end > applied.token_start,
        )
    ).all()
    for row in overlapping:
        row.status = "stale"
        row.resolved_at = utcnow_iso()


def _project_of_version(session: Session, version_id: str) -> str:
    from animatter.models import TranscriptVersion

    version = session.get(TranscriptVersion, version_id)
    return version.project_id if version is not None else ""


def _next_seq(session: Session, version_id: str) -> int:
    last = session.scalars(
        select(EditOp.seq).where(EditOp.version_id == version_id).order_by(EditOp.seq.desc())
    ).first()
    return (last or 0) + 1


def _rewrite_sentence_ranges(session: Session, version_id: str, ranges: list[tuple[int, int]]) -> None:
    rows = list(
        session.scalars(select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord))
    )
    for row, (start, end) in zip(rows, ranges, strict=False):
        row.token_start = start
        row.token_end = end
        row.updated_at = utcnow_iso()
    session.flush()


def _to_span(row: Token) -> TokenSpan:
    return TokenSpan(
        idx=row.idx,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        text=row.text,
        kind=row.kind,
        src_word_idx=row.src_word_idx,
        punc_after=row.punc_after,
        derived=row.derived,
        revised=row.revised,
    )


def persist_proposals(
    session: Session, version_id: str, proposals: Sequence[Proposal]
) -> list[CorrectionCandidate]:
    """Store one proof-reading run's proposals, skipping exact duplicates."""
    existing = {
        (row.token_start, row.original_text, row.proposed_text)
        for row in session.scalars(
            select(CorrectionCandidate).where(CorrectionCandidate.version_id == version_id)
        )
    }
    created: list[CorrectionCandidate] = []
    now = utcnow_iso()
    for proposal in proposals:
        key = (proposal.token_start, proposal.original, proposal.proposed)
        if key in existing:
            continue
        row = CorrectionCandidate(
            id=new_id("cor"),
            version_id=version_id,
            sentence_ord=proposal.sentence_ord,
            token_start=proposal.token_start,
            token_end=proposal.token_end,
            original_text=proposal.original,
            proposed_text=proposal.proposed,
            matched_hotword=proposal.matched_hotword,
            confidence=proposal.confidence,
            reason=proposal.reason,
            status="pending",
            source="llm",
            created_at=now,
        )
        session.add(row)
        created.append(row)
        existing.add(key)
    session.flush()
    return created
