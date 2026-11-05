"""Speaker identity inside a project.

Diarization numbers speakers per project; the editor needs those numbers turned
into names and colours the user can recognise. This module owns that mapping and
the "assign these sentences to that speaker" operation.

The design point worth stating: ``sentence.speaker_id`` stores the speaker's
**local integer**, not the ``speaker.id`` string. Renaming therefore touches one
row, and a project with no diarization can still create speakers by hand — which
is the fallback when the ASR model does not separate voices at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import Project, Sentence, Speaker

log = get_logger(__name__)

#: Assigned round-robin so adjacent speakers never share a hue. Chosen to stay
#: distinguishable in both light and dark themes.
PALETTE = ("#2563eb", "#db2777", "#16a34a", "#d97706", "#7c3aed", "#0891b2")

DEFAULT_NAMES = tuple(f"说话人{i + 1}" for i in range(8))

MAX_NAME_LENGTH = 24


@dataclass(frozen=True)
class SpeakerView:
    local_id: int
    name: str
    color: str
    sentence_count: int


class SpeakerError(AppError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__("speaker_invalid", message, hint=hint)


def _project_or_raise(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise AppError("project_not_found", "项目不存在", status=404)
    return project


def list_speakers(session: Session, project_id: str) -> list[SpeakerView]:
    """Speakers known to the project, plus any stragglers referenced by sentences.

    A sentence can carry a ``speaker_id`` with no ``speaker`` row (older data, or
    a diarizer that emitted an id without config). Synthesising a view for it
    keeps the UI honest instead of showing "说话人 2" for one sentence and
    nothing for the next.
    """
    _project_or_raise(session, project_id)
    rows = list(
        session.scalars(select(Speaker).where(Speaker.project_id == project_id).order_by(Speaker.local_id))
    )
    counts = _sentence_counts(session, project_id)

    known = {row.local_id: row for row in rows}
    ids = set(known) | set(counts)

    views: list[SpeakerView] = []
    for local_id in sorted(ids):
        row = known.get(local_id)
        views.append(
            SpeakerView(
                local_id=local_id,
                name=(row.name if row and row.name else _default_name(local_id)),
                color=(row.color if row and row.color else _default_color(local_id)),
                sentence_count=counts.get(local_id, 0),
            )
        )
    return views


def _sentence_counts(session: Session, project_id: str) -> dict[int, int]:
    """How many sentences each speaker has, for the whole project.

    Joined through ``transcript_version`` because ``sentence`` carries no
    ``project_id`` — going via the version is the only correct path, and skipping
    it would count every project's sentences together.
    """
    from animatter.models import TranscriptVersion

    counts: dict[int, int] = {}
    rows = session.execute(
        select(Sentence.speaker_id)
        .join(TranscriptVersion, TranscriptVersion.id == Sentence.version_id)
        .where(
            TranscriptVersion.project_id == project_id,
            Sentence.speaker_id.is_not(None),
        )
    ).all()
    for (speaker_id,) in rows:
        if speaker_id is None:
            continue
        counts[speaker_id] = counts.get(speaker_id, 0) + 1
    return counts


def _default_name(local_id: int) -> str:
    if local_id < len(DEFAULT_NAMES):
        return DEFAULT_NAMES[local_id]
    return f"说话人{local_id + 1}"


def _default_color(local_id: int) -> str:
    return PALETTE[local_id % len(PALETTE)]


def ensure_speaker(session: Session, project_id: str, local_id: int) -> Speaker:
    """Get or create the row for ``local_id``."""
    row = session.scalars(
        select(Speaker).where(Speaker.project_id == project_id, Speaker.local_id == local_id)
    ).first()
    if row is not None:
        return row

    row = Speaker(
        id=new_id("spk"),
        project_id=project_id,
        local_id=local_id,
        name=_default_name(local_id),
        color=_default_color(local_id),
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def create_speaker(session: Session, project_id: str, name: str = "") -> SpeakerView:
    """Add a speaker by hand — the path taken when diarization produced nothing."""
    _project_or_raise(session, project_id)
    existing = list_speakers(session, project_id)
    used = {view.local_id for view in existing}
    local_id = 0
    while local_id in used:
        local_id += 1

    clean = name.strip()[:MAX_NAME_LENGTH]
    row = Speaker(
        id=new_id("spk"),
        project_id=project_id,
        local_id=local_id,
        name=clean or _default_name(local_id),
        color=_default_color(local_id),
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    log.info("speaker.created", project=project_id, local_id=local_id)
    return SpeakerView(local_id=local_id, name=row.name, color=row.color, sentence_count=0)


def rename_speaker(
    session: Session, project_id: str, local_id: int, name: str, color: str | None = None
) -> SpeakerView:
    clean = name.strip()
    if not clean:
        raise SpeakerError("说话人名字不能为空", "可以用「主持人」「嘉宾 A」这类称呼。")
    if len(clean) > MAX_NAME_LENGTH:
        raise SpeakerError(f"名字最多 {MAX_NAME_LENGTH} 个字符")

    row = ensure_speaker(session, project_id, local_id)
    row.name = clean
    if color:
        row.color = color
    session.flush()

    counts = _sentence_counts(session, project_id)
    return SpeakerView(
        local_id=local_id,
        name=row.name,
        color=row.color,
        sentence_count=counts.get(local_id, 0),
    )


def assign_sentences(
    session: Session,
    version_id: str,
    sentence_ords: list[int],
    local_id: int | None,
) -> int:
    """Point the given sentences at a speaker (``None`` clears the assignment).

    Works even when the project has no diarization at all, which is the case the
    user hits with a model that cannot separate voices.
    """
    if not sentence_ords:
        raise SpeakerError("没有选中任何句子")

    rows = list(
        session.scalars(
            select(Sentence).where(Sentence.version_id == version_id, Sentence.ord.in_(sentence_ords))
        )
    )
    found = {row.ord for row in rows}
    missing = sorted(set(sentence_ords) - found)
    if missing:
        raise SpeakerError(f"句子 {missing} 不存在")

    for row in rows:
        row.speaker_id = local_id
        row.updated_at = utcnow_iso()
    session.flush()
    log.info("speaker.assigned", version=version_id, sentences=len(rows), local_id=local_id)
    return len(rows)


def delete_speaker(session: Session, project_id: str, local_id: int) -> None:
    """Forget the speaker's name/colour.

    Sentences keep their ``speaker_id``: dropping the annotation would silently
    rewrite data the user never asked to change, and the view synthesises a
    default name for the leftover id anyway. That is also why a local_id that
    only exists as an annotation is *not* a 404 — the UI lists it, so removing it
    has to be possible. Only an id nothing refers to is an error.
    """
    _project_or_raise(session, project_id)
    row = session.scalars(
        select(Speaker).where(Speaker.project_id == project_id, Speaker.local_id == local_id)
    ).first()
    if row is None:
        if local_id not in _sentence_counts(session, project_id):
            raise AppError("speaker_not_found", "该说话人不存在", status=404)
        return
    session.delete(row)
    session.flush()


def sync_with_diarization(session: Session, project_id: str, local_ids: set[int]) -> None:
    """Record every speaker id the ASR produced, so the UI can show them all."""
    for local_id in sorted(local_ids):
        ensure_speaker(session, project_id, local_id)
