"""Hotword glossary storage: the global library and the per-episode list.

The global library maps to a pre-compiled vocabulary list on Bailian, which has
a hard quota of 50 lists per account and a 5-minute update cooldown. Those two
facts are why this module tracks a content hash and a "next allowed" timestamp
instead of syncing on every write (AGENT.md §2.1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from animatter.core.hotwords import (
    DEFAULT_WEIGHT,
    HotwordCandidate,
    SanitizeReport,
    sanitize,
    to_instant_vocabulary,
)
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import EpisodeHotword, Hotword, HotwordSync

log = get_logger(__name__)

#: Bailian allows 50 lists per account; updates inside 5 minutes may not apply.
MAX_VOCABULARY_LISTS = 50
SYNC_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class SyncState:
    vocabulary_id: str
    target_model: str
    dirty: bool
    last_synced_at: str | None
    next_allowed_at: str | None
    content_hash: str


def _hash_content(items: list[tuple[str, int]]) -> str:
    payload = json.dumps(sorted(items), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_from_row(row: Hotword) -> HotwordCandidate:
    return HotwordCandidate(
        text=row.text,
        weight=row.weight,
        lang=row.lang,
        kind="term",
        evidence="",
        origin="global" if row.project_id is None else "project",
    )


# --- global glossary ----------------------------------------------------


def list_global(session: Session, *, include_disabled: bool = False) -> list[Hotword]:
    stmt = select(Hotword).where(Hotword.project_id.is_(None))
    if not include_disabled:
        stmt = stmt.where(Hotword.enabled.is_(True))
    return list(session.scalars(stmt.order_by(Hotword.weight.desc(), Hotword.text)))


def _upsert(
    session: Session,
    *,
    project_id: str | None,
    candidate: HotwordCandidate,
) -> Hotword:
    existing = session.scalars(
        select(Hotword).where(Hotword.project_id == project_id, Hotword.text == candidate.text)
    ).first()
    now = utcnow_iso()
    if existing is not None:
        existing.weight = candidate.weight
        existing.lang = candidate.lang
        existing.enabled = True
        existing.updated_at = now
        return existing

    row = Hotword(
        id=new_id("hw"),
        project_id=project_id,
        text=candidate.text,
        weight=candidate.weight,
        lang=candidate.lang,
        enabled=True,
        tags_json="[]",
        hit_count=0,
        miss_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def add_global(session: Session, candidates: list[HotwordCandidate], *, limit: int = 450) -> SanitizeReport:
    """Add or update terms in the shared glossary, then mark the remote copy dirty."""
    report = sanitize(candidates, limit=limit)
    for candidate in report.accepted:
        _upsert(session, project_id=None, candidate=candidate)
    session.flush()
    _mark_dirty(session, project_id=None)
    log.info("hotwords.global_added", accepted=len(report.accepted), rejected=len(report.rejected))
    return report


def remove_global(session: Session, text: str) -> None:
    row = session.scalars(select(Hotword).where(Hotword.project_id.is_(None), Hotword.text == text)).first()
    if row is None:
        raise AppError("hotword_not_found", f"词库中没有「{text}」", hint="刷新词库后重试。", status=404)
    session.delete(row)
    session.flush()
    _mark_dirty(session, project_id=None)


def record_hit(session: Session, text: str, *, delta: int = 1) -> None:
    """Credit a term that a human confirmed — this is the feedback loop that
    makes the glossary improve without manual curation (AGENT.md §6.1)."""
    row = session.scalars(select(Hotword).where(Hotword.project_id.is_(None), Hotword.text == text)).first()
    if row is not None:
        row.hit_count += delta
        row.updated_at = utcnow_iso()


def record_miss(session: Session, text: str, *, delta: int = 1) -> None:
    row = session.scalars(select(Hotword).where(Hotword.project_id.is_(None), Hotword.text == text)).first()
    if row is not None:
        row.miss_count += delta
        row.updated_at = utcnow_iso()


# --- pre-compiled vocabulary sync --------------------------------------


def _sync_row(session: Session, project_id: str | None) -> HotwordSync:
    condition = (
        HotwordSync.project_id.is_(None) if project_id is None else HotwordSync.project_id == project_id
    )
    row = session.scalars(select(HotwordSync).where(condition)).first()
    if row is None:
        row = HotwordSync(
            id=new_id("hws"),
            project_id=project_id,
            vocabulary_id="",
            target_model="",
            content_hash="",
            dirty=True,
            last_synced_at=None,
            next_allowed_at=None,
        )
        session.add(row)
        session.flush()
    return row


def _mark_dirty(session: Session, *, project_id: str | None) -> None:
    row = _sync_row(session, project_id)
    rows = list_global(session)
    row.content_hash = _hash_content([(r.text, r.weight) for r in rows])
    row.dirty = True
    session.flush()


def sync_state(session: Session, *, project_id: str | None = None) -> SyncState:
    row = _sync_row(session, project_id)
    return SyncState(
        vocabulary_id=row.vocabulary_id,
        target_model=row.target_model,
        dirty=row.dirty,
        last_synced_at=row.last_synced_at,
        next_allowed_at=row.next_allowed_at,
        content_hash=row.content_hash,
    )


def mark_synced(session: Session, *, project_id: str | None, vocabulary_id: str, target_model: str) -> None:
    row = _sync_row(session, project_id)
    now = utcnow_iso()
    row.vocabulary_id = vocabulary_id
    row.target_model = target_model
    row.dirty = False
    row.last_synced_at = now
    # Bailian advises ≥5 minutes between updates; record it so the UI can
    # explain why a change has not taken effect yet instead of looking broken.
    row.next_allowed_at = _plus_seconds(now, SYNC_COOLDOWN_SECONDS)
    session.flush()


def _plus_seconds(timestamp: str, seconds: int) -> str:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def global_vocabulary(session: Session) -> list[HotwordCandidate]:
    return [_candidate_from_row(row) for row in list_global(session)]


# --- per-episode list ---------------------------------------------------


def list_episode(session: Session, project_id: str, media_id: str) -> list[EpisodeHotword]:
    stmt = (
        select(EpisodeHotword)
        .where(EpisodeHotword.project_id == project_id, EpisodeHotword.media_id == media_id)
        .order_by(EpisodeHotword.weight.desc(), EpisodeHotword.text)
    )
    return list(session.scalars(stmt))


def replace_episode(
    session: Session,
    *,
    project_id: str,
    media_id: str,
    candidates: list[HotwordCandidate],
    limit: int = 450,
) -> SanitizeReport:
    """Replace the whole per-episode list (the outline regenerates it wholesale)."""
    report = sanitize(candidates, limit=limit)
    session.execute(
        delete(EpisodeHotword).where(
            EpisodeHotword.project_id == project_id, EpisodeHotword.media_id == media_id
        )
    )
    for candidate in report.accepted:
        session.add(
            EpisodeHotword(
                id=new_id("ehw"),
                project_id=project_id,
                media_id=media_id,
                text=candidate.text,
                weight=candidate.weight,
                lang=candidate.lang,
                origin=candidate.origin,
                evidence=candidate.evidence,
                enabled=True,
            )
        )
    session.flush()
    log.info("hotwords.episode_replaced", project_id=project_id, count=len(report.accepted))
    return report


def effective_vocabulary(
    session: Session, *, project_id: str, media_id: str, limit: int = 450
) -> tuple[dict[str, int], int]:
    """Global + episode terms merged for one recognition call.

    Returns the vocabulary and how many terms were trimmed; the caller records
    the trimmed payload in ``transcribe_job.params_json`` so a run stays
    reproducible even after the glossary changes.
    """
    from animatter.core.hotwords import merge_vocabularies, truncate_to_limit

    globals_ = to_instant_vocabulary(global_vocabulary(session))
    episode = {row.text: row.weight for row in list_episode(session, project_id, media_id) if row.enabled}
    merged = merge_vocabularies(globals_, episode)
    return truncate_to_limit(merged, limit=limit)


def default_weight() -> int:
    return DEFAULT_WEIGHT
