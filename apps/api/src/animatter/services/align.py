"""Shift a whole transcript timeline by a constant.

Why this exists
---------------
Bailian's word timestamps run late. Measured on two real episodes (997 sentence
heads with a genuine pause before them, acoustic onset vs the reported
``begin_time``): **median −65 ms, 27 % of them more than 100 ms late**. The
16 kHz/MP3 transcode is provably not the cause — decoding both files and
cross-correlating gives a shift of 0 samples, and transcribing the same 60 s as
MP3 and as WAV gives identical times (median difference 0 ms). It is the model.

A per-word correction is not on the table: the model reports 10 ms granularity
and the individual error is not reproducible from the audio alone. What *is*
available is a constant, which is what a systematic late bias calls for.

The rule this module exists to enforce
--------------------------------------
**Apply the difference to the target, never the step.** The API takes an
absolute offset (``shift_ms``) and applies ``target - current``. Two reasons:

* The UI can therefore offer 「对齐到 −80」 and 「撤销」 (＝ align to 0) without
  the user having to know how many times they pressed it.
* Re-applying is idempotent, so a double click cannot double the shift.

Two consequences worth stating, because they are the non-obvious part:

1. **Human edge drags survive.** Because only the delta is applied, a token the
   user has already moved keeps its *relative* position. Recomputing every time
   from ``transcripts/*.raw.json`` would silently undo those drags.
2. **Everything on the timeline moves together.** Tokens and deletions both
   shift, so a cut point stays attached to the same syllable. Shifting only the
   tokens would slide the deletions off the very words they were cut for.

The shift is **clamped, not truncated**: if ``-1000`` would push the first token
before zero, the effective shift stops where the earliest timestamp reaches
zero. Clamping the delta keeps the whole timeline rigid; clamping individual
rows would silently compress the head of the recording.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.log import get_logger
from animatter.models import Deletion, Token, TranscriptVersion

log = get_logger(__name__)

#: Sanity bounds. A real offset is tens of milliseconds; anything past a second
#: is a typo, and applying it would be tedious to undo.
MIN_SHIFT_MS = -1000
MAX_SHIFT_MS = 1000


class AlignError(Exception):
    """The shift could not be applied; the message is shown to the user."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ShiftResult:
    """What happened, so the caller can report it honestly."""

    requested_ms: int
    applied_ms: int
    previous_ms: int
    tokens: int
    deletions: int
    #: Set when the requested value had to be reduced to keep times >= 0 or
    #: <= the media duration.
    clamped: bool
    reason: str

    @property
    def delta_ms(self) -> int:
        return self.applied_ms - self.previous_ms


def _bounds(session: Session, version: TranscriptVersion) -> tuple[int, int]:
    """Earliest and latest timestamp anywhere in this version's timeline."""
    first_token = session.scalars(
        select(Token.start_ms).where(Token.version_id == version.id).order_by(Token.start_ms)
    ).first()
    last_token = session.scalars(
        select(Token.end_ms).where(Token.version_id == version.id).order_by(Token.end_ms.desc())
    ).first()
    first_deletion = session.scalars(
        select(Deletion.start_ms).where(Deletion.version_id == version.id).order_by(Deletion.start_ms)
    ).first()
    last_deletion = session.scalars(
        select(Deletion.end_ms).where(Deletion.version_id == version.id).order_by(Deletion.end_ms.desc())
    ).first()

    starts = [value for value in (first_token, first_deletion) if value is not None]
    ends = [value for value in (last_token, last_deletion) if value is not None]
    return (min(starts) if starts else 0, max(ends) if ends else 0)


def apply_shift(session: Session, version_id: str, *, shift_ms: int) -> ShiftResult:
    """Make this version's timeline sit at ``shift_ms`` relative to the ASR's own."""
    if not MIN_SHIFT_MS <= shift_ms <= MAX_SHIFT_MS:
        raise AlignError(
            "time_shift_out_of_range",
            f"偏移量必须在 {MIN_SHIFT_MS} 到 {MAX_SHIFT_MS} ms 之间",
            "这个范围的跨度已经超过一秒，通常说明输错了。",
        )

    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AlignError("version_not_found", "转写版本不存在", "刷新版本列表。")

    current = version.time_shift_ms
    requested_delta = shift_ms - current
    if requested_delta == 0:
        tokens = session.scalars(select(Token).where(Token.version_id == version_id)).all()
        deletions = session.scalars(select(Deletion).where(Deletion.version_id == version_id)).all()
        return ShiftResult(shift_ms, current, current, len(tokens), len(deletions), False, "")

    earliest, latest = _bounds(session, version)
    delta = requested_delta
    reason = ""

    # Keep every timestamp inside [0, duration]. The delta is clamped, not the
    # rows: a rigid shift keeps the transcript internally consistent, while
    # clamping rows one by one would squeeze the head of the file.
    if delta < 0 and earliest + delta < 0:
        delta = -earliest
        reason = f"最早的词在 {earliest} ms，再往前会变成负数"
    duration = version.duration_ms
    if delta > 0 and duration and latest + delta > duration:
        delta = max(0, duration - latest)
        reason = f"最晚的词在 {latest} ms，再往后会超出音频长度 {duration} ms"

    applied = current + delta

    token_rows = session.scalars(select(Token).where(Token.version_id == version_id)).all()
    for token in token_rows:
        token.start_ms += delta
        token.end_ms += delta

    deletion_rows = session.scalars(select(Deletion).where(Deletion.version_id == version_id)).all()
    for deletion in deletion_rows:
        deletion.start_ms += delta
        deletion.end_ms += delta

    version.time_shift_ms = applied
    session.flush()
    log.info(
        "align.shift",
        version_id=version_id,
        requested=shift_ms,
        applied=applied,
        delta=delta,
        tokens=len(token_rows),
    )
    return ShiftResult(
        requested_ms=shift_ms,
        applied_ms=applied,
        previous_ms=current,
        tokens=len(token_rows),
        deletions=len(deletion_rows),
        clamped=delta != requested_delta,
        reason=reason,
    )


def describe_shift(session: Session, version_id: str) -> dict[str, int]:
    """Current offset for the panel header."""
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AlignError("version_not_found", "转写版本不存在", "刷新版本列表。")
    earliest, latest = _bounds(session, version)
    return {
        "time_shift_ms": version.time_shift_ms,
        "earliest_ms": earliest,
        "latest_ms": latest,
        "duration_ms": version.duration_ms,
    }
