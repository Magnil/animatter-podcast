"""Proof-reading endpoints: propose, review, accept or reject corrections."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import TranscriptVersion
from animatter.providers.http import build_client
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import corrections as correction_service
from animatter.services import hotwords as hotword_service
from animatter.services import proofread as proofread_service
from animatter.services import settings_store
from animatter.services import transcribe as transcribe_service

router = APIRouter(prefix="/api", tags=["proofread"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class CandidateOut(BaseModel):
    id: str
    sentence_ord: int
    token_start: int
    token_end: int
    original_text: str
    proposed_text: str
    matched_hotword: str
    confidence: float
    reason: str
    status: str


class ProofreadIn(BaseModel):
    project_id: str
    media_id: str
    #: ``None`` → use the saved 设置 → 热词与校对 value, so the dialog and the
    #: request cannot drift apart.
    chunk_size: int | None = None
    dry_run: bool = False


class ProofreadOut(BaseModel):
    candidates: list[CandidateOut]
    proposed: int
    rejected: list[dict[str, str]]
    prompt_tokens: int
    completion_tokens: int
    chunks: int


class ApplyOut(BaseModel):
    candidate: CandidateOut
    token_count: int


def _llm(request: Request) -> BailianLlm:
    settings: Settings = request.app.state.settings
    if not settings.dashscope_api_key:
        raise AppError(
            "missing_api_key",
            "未配置 DASHSCOPE_API_KEY",
            hint="在 .env 里填入百炼 API Key，重启服务后再试。",
            status=503,
        )
    return BailianLlm(
        base_url=settings.dashscope_compatible_base,
        api_key=settings.dashscope_api_key,
        model=settings.llm_model,
    )


def _to_out(row: object) -> CandidateOut:
    return CandidateOut(
        id=row.id,  # type: ignore[attr-defined]
        sentence_ord=row.sentence_ord,  # type: ignore[attr-defined]
        token_start=row.token_start,  # type: ignore[attr-defined]
        token_end=row.token_end,  # type: ignore[attr-defined]
        original_text=row.original_text,  # type: ignore[attr-defined]
        proposed_text=row.proposed_text,  # type: ignore[attr-defined]
        matched_hotword=row.matched_hotword,  # type: ignore[attr-defined]
        confidence=row.confidence,  # type: ignore[attr-defined]
        reason=row.reason,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
    )


@router.post("/versions/{version_id}/proofread", response_model=ProofreadOut)
def proofread(
    version_id: str,
    payload: ProofreadIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> ProofreadOut:
    """Run one proof-reading pass and store the candidates.

    Candidates are proposals, never edits: acceptance is a separate call so a
    human decides what enters the transcript.
    """
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)
    if version.project_id != payload.project_id:
        raise AppError("version_project_mismatch", "该版本不属于这个项目")

    if not settings_store.get_setting(session, "use_proofread"):
        raise AppError(
            "proofread_disabled",
            "LLM 校对已在设置里关闭",
            hint="打开「设置 → 热词与校对 → 启用 LLM 校对」后再试。",
        )

    vocabulary, _trimmed = hotword_service.effective_vocabulary(
        session, project_id=payload.project_id, media_id=payload.media_id
    )
    if not vocabulary:
        raise AppError(
            "no_hotwords",
            "热词表为空，无法校对",
            hint="校对以热词为唯一依据。先在词库添加词条，或从大纲生成热词。",
        )

    tokens = transcribe_service.load_tokens(session, version_id)
    sentences = transcribe_service.load_sentences(session, version_id)

    chunk_size = payload.chunk_size or int(settings_store.get_setting(session, "proofread_chunk_size"))
    llm = _llm(request)
    with build_client() as client:
        outcome = proofread_service.review(
            tokens=tokens,
            sentences=sentences,
            hotwords=vocabulary,
            llm=llm,
            client=client,
            chunk_size=max(5, min(chunk_size, 50)),
        )

    stored = (
        []
        if payload.dry_run
        else correction_service.persist_proposals(session, version_id, outcome.proposals)
    )

    return ProofreadOut(
        candidates=[_to_out(row) for row in stored]
        if stored
        else [
            CandidateOut(
                id="",
                sentence_ord=p.sentence_ord,
                token_start=p.token_start,
                token_end=p.token_end,
                original_text=p.original,
                proposed_text=p.proposed,
                matched_hotword=p.matched_hotword,
                confidence=p.confidence,
                reason=p.reason,
                status="preview",
            )
            for p in outcome.proposals
        ],
        proposed=len(outcome.proposals),
        rejected=[{"item": item, "reason": reason} for item, reason in outcome.rejected],
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        chunks=outcome.chunks,
    )


@router.get("/versions/{version_id}/corrections", response_model=list[CandidateOut])
def list_corrections(
    version_id: str,
    session: Session = Depends(_session_dependency),
    status: str | None = "pending",
) -> list[CandidateOut]:
    rows = correction_service.list_candidates(session, version_id, status=None if status == "all" else status)
    return [_to_out(row) for row in rows]


@router.post("/corrections/{candidate_id}/accept", response_model=ApplyOut)
def accept_correction(candidate_id: str, session: Session = Depends(_session_dependency)) -> ApplyOut:
    candidate = correction_service.apply_candidate(session, candidate_id)
    tokens = transcribe_service.load_tokens(session, candidate.version_id)

    # The feedback loop: a human-confirmed fix teaches the glossary which terms
    # the ASR actually gets wrong (AGENTS.md §6.1).
    if candidate.matched_hotword:
        hotword_service.record_hit(session, candidate.matched_hotword)

    return ApplyOut(candidate=_to_out(candidate), token_count=len(tokens))


@router.post("/corrections/{candidate_id}/reject", response_model=CandidateOut)
def reject_correction(candidate_id: str, session: Session = Depends(_session_dependency)) -> CandidateOut:
    candidate = correction_service.reject_candidate(session, candidate_id)
    if candidate.matched_hotword:
        # Rejected often means "the ASR was right, the hotword is a false friend".
        hotword_service.record_miss(session, candidate.matched_hotword)
    return _to_out(candidate)
