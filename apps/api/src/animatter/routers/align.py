"""Timeline alignment endpoints (设置 → 时间轴对齐).

The measurement behind this feature lives in ``services/align.py``; this module
is only the transport. Two rules shaped the shape of the API:

* **Absolute, not relative.** ``POST .../time-shift {"shift_ms": -80}`` means
  "make this version sit 80 ms earlier than the ASR reported", not "move it 80 ms
  earlier". Applying the same request twice is therefore a no-op instead of a
  160 ms shift, which is what allows the panel to have a 「撤销」 that the user
  never has to reason about.
* **The response reports the clamp.** Asking for −1000 on a file whose first
  word is at 240 ms cannot be honoured; saying so in the response is the
  difference between a working feature and a silently wrong timeline.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.db import Database
from animatter.errors import AppError
from animatter.services import align as align_service

router = APIRouter(prefix="/api", tags=["align"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class ShiftIn(BaseModel):
    #: Negative moves the transcript earlier (the usual case: the ASR was late).
    shift_ms: int


class ShiftOut(BaseModel):
    shift_ms: int
    previous_ms: int
    delta_ms: int
    tokens: int
    deletions: int
    clamped: bool
    reason: str


class ShiftStateOut(BaseModel):
    time_shift_ms: int
    earliest_ms: int
    latest_ms: int
    duration_ms: int


def _raise(exc: align_service.AlignError) -> None:
    status = 404 if exc.code == "version_not_found" else 400
    raise AppError(exc.code, exc.message, hint=exc.hint, status=status) from exc


@router.get("/versions/{version_id}/time-shift", response_model=ShiftStateOut)
def get_time_shift(
    version_id: str, session: Session = Depends(_session_dependency)
) -> ShiftStateOut:
    try:
        return ShiftStateOut(**align_service.describe_shift(session, version_id))
    except align_service.AlignError as exc:
        _raise(exc)
        raise  # unreachable; keeps the type checker happy


@router.post("/versions/{version_id}/time-shift", response_model=ShiftOut)
def set_time_shift(
    version_id: str,
    payload: ShiftIn,
    session: Session = Depends(_session_dependency),
) -> ShiftOut:
    try:
        result = align_service.apply_shift(session, version_id, shift_ms=payload.shift_ms)
    except align_service.AlignError as exc:
        _raise(exc)
        raise
    return ShiftOut(
        shift_ms=result.applied_ms,
        previous_ms=result.previous_ms,
        delta_ms=result.delta_ms,
        tokens=result.tokens,
        deletions=result.deletions,
        clamped=result.clamped,
        reason=result.reason,
    )
