"""Hotword endpoints: global glossary, per-episode list, and outline extraction."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.hotwords import DEFAULT_WEIGHT, HotwordCandidate, sanitize
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import hotwords as hotword_service
from animatter.services import outline as outline_service

router = APIRouter(prefix="/api", tags=["hotwords"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


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


class TermIn(BaseModel):
    text: str = Field(min_length=1, max_length=64)
    weight: int = Field(default=DEFAULT_WEIGHT, ge=1, le=50)
    lang: str = "zh"
    kind: str = "term"
    evidence: str = ""


class TermOut(BaseModel):
    text: str
    weight: int
    lang: str
    enabled: bool
    hit_count: int = 0
    miss_count: int = 0


class RejectedOut(BaseModel):
    text: str
    reason: str


class HotwordWriteOut(BaseModel):
    accepted: list[TermOut]
    rejected: list[RejectedOut]
    downweighted: list[dict[str, object]]


class OutlineIn(BaseModel):
    content: str
    meta: str = ""


class SuggestOut(BaseModel):
    terms: list[TermOut]
    rejected: list[RejectedOut]
    downweighted: list[dict[str, object]]
    warnings: list[str]
    prompt_tokens: int
    completion_tokens: int
    truncated: bool
    raw_count: int


@router.get("/hotwords", response_model=list[TermOut])
def list_global_hotwords(
    session: Session = Depends(_session_dependency),
    include_disabled: bool = False,
) -> list[TermOut]:
    return [
        TermOut(
            text=row.text,
            weight=row.weight,
            lang=row.lang,
            enabled=row.enabled,
            hit_count=row.hit_count,
            miss_count=row.miss_count,
        )
        for row in hotword_service.list_global(session, include_disabled=include_disabled)
    ]


@router.post("/hotwords", response_model=HotwordWriteOut, status_code=201)
def add_global_hotwords(
    terms: list[TermIn], session: Session = Depends(_session_dependency)
) -> HotwordWriteOut:
    candidates = [
        HotwordCandidate(
            text=t.text,
            weight=t.weight,
            lang=t.lang,
            kind=t.kind,
            evidence=t.evidence,
            origin="manual",
        )
        for t in terms
    ]
    report = hotword_service.add_global(session, candidates)
    rows = {row.text: row for row in hotword_service.list_global(session)}
    return HotwordWriteOut(
        accepted=[
            TermOut(
                text=c.text,
                weight=c.weight,
                lang=c.lang,
                enabled=True,
                hit_count=rows[c.text].hit_count if c.text in rows else 0,
                miss_count=rows[c.text].miss_count if c.text in rows else 0,
            )
            for c in report.accepted
        ],
        rejected=[RejectedOut(text=text, reason=reason) for text, reason in report.rejected],
        downweighted=[{"text": text, "from": old, "to": new} for text, old, new in report.downweighted],
    )


@router.delete("/hotwords/{text}", status_code=204)
def delete_global_hotword(text: str, session: Session = Depends(_session_dependency)) -> None:
    hotword_service.remove_global(session, text)


@router.get("/projects/{project_id}/media/{media_id}/hotwords", response_model=list[TermOut])
def list_episode_hotwords(
    project_id: str, media_id: str, session: Session = Depends(_session_dependency)
) -> list[TermOut]:
    return [
        TermOut(text=row.text, weight=row.weight, lang=row.lang, enabled=row.enabled)
        for row in hotword_service.list_episode(session, project_id, media_id)
    ]


@router.post(
    "/projects/{project_id}/media/{media_id}/hotwords",
    response_model=HotwordWriteOut,
    status_code=201,
)
def replace_episode_hotwords(
    project_id: str,
    media_id: str,
    terms: list[TermIn],
    session: Session = Depends(_session_dependency),
) -> HotwordWriteOut:
    candidates = [
        HotwordCandidate(
            text=t.text,
            weight=t.weight,
            lang=t.lang,
            kind=t.kind,
            evidence=t.evidence,
            origin="manual",
        )
        for t in terms
    ]
    report = hotword_service.replace_episode(
        session, project_id=project_id, media_id=media_id, candidates=candidates
    )
    return HotwordWriteOut(
        accepted=[TermOut(text=c.text, weight=c.weight, lang=c.lang, enabled=True) for c in report.accepted],
        rejected=[RejectedOut(text=text, reason=reason) for text, reason in report.rejected],
        downweighted=[{"text": text, "from": old, "to": new} for text, old, new in report.downweighted],
    )


@router.post(
    "/projects/{project_id}/media/{media_id}/hotwords/append",
    response_model=HotwordWriteOut,
    status_code=201,
)
def append_episode_hotwords(
    project_id: str,
    media_id: str,
    terms: list[TermIn],
    session: Session = Depends(_session_dependency),
) -> HotwordWriteOut:
    """Add terms to an episode's list without disturbing the ones already there.

    Distinct from the replace endpoint above, which exists so outline extraction
    can regenerate the list wholesale. "Add one word" posted to *that* endpoint
    silently deletes every other term, which is exactly the sort of thing that
    looks like a UI bug and is really an API design bug.
    """
    candidates = [
        HotwordCandidate(
            text=t.text,
            weight=t.weight,
            lang=t.lang,
            kind=t.kind,
            evidence=t.evidence,
            origin="manual",
        )
        for t in terms
    ]
    report = hotword_service.append_episode(
        session, project_id=project_id, media_id=media_id, candidates=candidates
    )
    return HotwordWriteOut(
        accepted=[TermOut(text=c.text, weight=c.weight, lang=c.lang, enabled=True) for c in report.accepted],
        rejected=[RejectedOut(text=text, reason=reason) for text, reason in report.rejected],
        downweighted=[{"text": text, "from": old, "to": new} for text, old, new in report.downweighted],
    )


@router.delete("/projects/{project_id}/media/{media_id}/hotwords/{text}", status_code=204)
def delete_episode_hotword(
    project_id: str,
    media_id: str,
    text: str,
    session: Session = Depends(_session_dependency),
) -> None:
    """Remove one term from this episode's list.

    The client used to delete by re-POSTing the whole list, which is fine until
    two removals race; a real DELETE keeps the operation as narrow as the click.
    """
    hotword_service.remove_episode(session, project_id=project_id, media_id=media_id, text=text)


@router.get("/projects/{project_id}/media/{media_id}/vocabulary")
def episode_vocabulary(
    project_id: str, media_id: str, session: Session = Depends(_session_dependency)
) -> dict[str, object]:
    """The exact payload that would be sent as ``parameters.vocabulary``."""
    vocabulary, trimmed = hotword_service.effective_vocabulary(
        session, project_id=project_id, media_id=media_id
    )
    return {"vocabulary": vocabulary, "count": len(vocabulary), "trimmed": trimmed}


@router.put("/projects/{project_id}/media/{media_id}/outline")
def put_outline(
    project_id: str,
    media_id: str,
    payload: OutlineIn,
    session: Session = Depends(_session_dependency),
) -> dict[str, object]:
    row = outline_service.save_outline(
        session, project_id=project_id, media_id=media_id, content=payload.content
    )
    return {"outline_id": row.id, "length": len(row.content)}


@router.get("/projects/{project_id}/media/{media_id}/outline")
def get_outline(
    project_id: str, media_id: str, session: Session = Depends(_session_dependency)
) -> dict[str, object]:
    content = outline_service.load_outline(session, project_id=project_id, media_id=media_id)
    return {"content": content, "length": len(content)}


@router.post(
    "/projects/{project_id}/media/{media_id}/hotwords/suggest",
    response_model=SuggestOut,
)
def suggest_from_outline(
    project_id: str,
    media_id: str,
    request: Request,
    payload: OutlineIn | None = None,
    session: Session = Depends(_session_dependency),
) -> SuggestOut:
    """Extract candidates from an outline.

    Returns candidates **without writing them**: extraction is a proposal, and
    the operator must be able to delete the wrong ones before they shape an ASR
    run (AGENT.md §6.1).
    """
    content = payload.content if payload is not None else ""
    meta = payload.meta if payload is not None else ""
    if not content.strip():
        content = outline_service.load_outline(session, project_id=project_id, media_id=media_id)
    if not content.strip():
        raise AppError(
            "outline_empty",
            "没有可用的节目大纲",
            hint="先 PUT 大纲内容，或在请求体里直接传 content。",
        )

    llm = _llm(request)
    from animatter.providers.http import build_client

    with build_client() as client:
        result = outline_service.extract(outline_text=content, llm=llm, client=client, meta=meta)

    if payload is not None and payload.content.strip():
        outline_service.save_outline(
            session, project_id=project_id, media_id=media_id, content=payload.content
        )

    return SuggestOut(
        terms=[TermOut(text=c.text, weight=c.weight, lang=c.lang, enabled=True) for c in result.candidates],
        rejected=[RejectedOut(text=text, reason=reason) for text, reason in result.report.rejected],
        downweighted=[
            {"text": text, "from": old, "to": new} for text, old, new in result.report.downweighted
        ],
        warnings=result.warnings,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        truncated=result.truncated,
        raw_count=result.raw_count,
    )


@router.post("/projects/{project_id}/media/{media_id}/hotwords/suggest/accept")
def accept_suggestions(
    project_id: str,
    media_id: str,
    terms: list[TermIn],
    session: Session = Depends(_session_dependency),
    also_global: bool = False,
) -> dict[str, object]:
    """Persist reviewed candidates as the episode list (optionally into the glossary)."""
    candidates = [
        HotwordCandidate(
            text=t.text,
            weight=t.weight,
            lang=t.lang,
            kind=t.kind,
            evidence=t.evidence,
            origin="outline",
        )
        for t in terms
    ]
    episode_report = hotword_service.replace_episode(
        session, project_id=project_id, media_id=media_id, candidates=candidates
    )
    global_count = 0
    if also_global:
        global_report = hotword_service.add_global(session, candidates)
        global_count = len(global_report.accepted)
    vocabulary, trimmed = hotword_service.effective_vocabulary(
        session, project_id=project_id, media_id=media_id
    )
    return {
        "episode_count": len(episode_report.accepted),
        "global_count": global_count,
        "vocabulary_count": len(vocabulary),
        "trimmed": trimmed,
    }


@router.get("/hotwords/validate")
def validate_term(text: str) -> dict[str, object]:
    """Check one term against the provider's documented limits before saving."""
    report = sanitize([HotwordCandidate(text=text)])
    return {
        "text": text,
        "ok": bool(report.accepted),
        "reason": report.rejected[0][1] if report.rejected else "",
        "weight": report.accepted[0].weight if report.accepted else None,
    }
