"""One-click cleanup endpoints: plan, apply, restore, and the undo hook.

``plan`` is the one that makes the feature honest. It is a GET-shaped read that
returns exactly what ``apply`` would cut, with per-kind counts, so the dialog can
show "口癖 12 处 · 静音 40 处 · 未转写 7 处" and let the user untick a category
before anything is written.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.log import get_logger
from animatter.services import cleanup as cleanup_service

router = APIRouter(prefix="/api", tags=["cleanup"])
log = get_logger(__name__)


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class CleanupItemOut(BaseModel):
    kind: str
    start_ms: int
    end_ms: int
    duration_ms: int
    text: str
    token_indices: list[int]
    sentence_ord: int


class SummaryOut(BaseModel):
    items: int
    duration_ms: int


class PlanOut(BaseModel):
    version_id: str
    duration_ms: int
    items: list[CleanupItemOut]
    summary: dict[str, SummaryOut]
    fillers: list[str]
    kinds: list[str]


class ApplyIn(BaseModel):
    #: Which kinds to cut. Omitted means all three.
    kinds: list[str] | None = None
    #: Per-request overrides; omitted means "use 设置".
    min_silence_ms: int | None = Field(default=None, ge=100, le=3000)
    silence_pad_ms: int | None = Field(default=None, ge=0, le=300)
    min_disfluency_ms: int | None = Field(default=None, ge=100, le=3000)


class ApplyOut(BaseModel):
    batch: str
    op_id: str
    deletions: int
    counts: dict[str, int]
    removed_ms: int
    kinds: list[str]


class RestoreOut(BaseModel):
    restored: int
    source: str | None
    batch: str | None


class CountsOut(BaseModel):
    counts: dict[str, int]


@router.get("/versions/{version_id}/cleanup/plan", response_model=PlanOut)
def get_plan(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    min_silence_ms: int | None = Query(default=None, ge=100, le=3000),
    silence_pad_ms: int | None = Query(default=None, ge=0, le=300),
    min_disfluency_ms: int | None = Query(default=None, ge=100, le=3000),
) -> PlanOut:
    """What cleanup would remove. Read-only — nothing is written.

    Thresholds can be overridden here so the dialog's sliders can re-plan as the
    user drags them without saving to 设置 first.
    """
    settings: Settings = request.app.state.settings
    view = cleanup_service.plan(
        session,
        settings,
        version_id,
        min_silence_ms=min_silence_ms,
        silence_pad_ms=silence_pad_ms,
        min_disfluency_ms=min_disfluency_ms,
    )
    return PlanOut(
        version_id=view.version_id,
        duration_ms=view.duration_ms,
        items=[CleanupItemOut(**item) for item in view.items],  # type: ignore[arg-type]
        summary={kind: SummaryOut(**value) for kind, value in view.summary.items()},
        fillers=view.fillers,
        kinds=list(cleanup_service.KINDS),
    )


@router.post("/versions/{version_id}/cleanup", response_model=ApplyOut, status_code=201)
def apply_cleanup(
    version_id: str,
    payload: ApplyIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> ApplyOut:
    """Commit one cleanup as a single undoable operation."""
    settings: Settings = request.app.state.settings
    outcome = cleanup_service.apply(
        session,
        settings,
        version_id,
        kinds=set(payload.kinds) if payload.kinds else None,
        min_silence_ms=payload.min_silence_ms,
        silence_pad_ms=payload.silence_pad_ms,
        min_disfluency_ms=payload.min_disfluency_ms,
    )
    return ApplyOut(**outcome)  # type: ignore[arg-type]


@router.post("/versions/{version_id}/cleanup/restore", response_model=RestoreOut)
def restore_cleanup(
    version_id: str,
    session: Session = Depends(_session_dependency),
    source: str | None = Query(default=None, description="按类别还原：filler / disfluency / silence"),
    batch: str | None = Query(default=None, description="按批次还原（一次清理）"),
) -> RestoreOut:
    outcome = cleanup_service.restore(session, version_id, source=source, batch=batch)
    return RestoreOut(**outcome)  # type: ignore[arg-type]


@router.get("/versions/{version_id}/cleanup/counts", response_model=CountsOut)
def get_counts(version_id: str, session: Session = Depends(_session_dependency)) -> CountsOut:
    """How many cleanups of each kind are currently in effect."""
    return CountsOut(counts=cleanup_service.counts_by_kind(session, version_id))
