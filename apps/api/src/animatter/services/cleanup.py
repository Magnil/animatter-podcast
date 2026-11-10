"""One-click cleanup: plan → preview → apply → undo as one operation.

Three things this module is responsible for, in order of how easy they are to
get wrong:

1. **The plan is a proposal, not a commit.** ``plan`` computes what *would* be
   cut and returns it with per-kind counts. Nothing is written until ``apply``.
   A one-click tool that rewrites the transcript before the user can see what it
   did is a tool people stop trusting after the first surprise.

2. **Applying is one undoable operation.** Four hundred deletions from one click
   must not become four hundred history entries. The batch id is stored in the
   edit-op payload, so undo is one call and the history panel shows one line.

3. **Nothing is silently re-added.** Planning skips tokens that are already
   deleted and ignores audio already covered by a deletion, so running cleanup
   twice does not double the count or propose the same cut again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.cleanup import (
    CleanupPlan,
    PlanRequest,
    TimeRange,
    build_plan,
)
from animatter.core.fillers import parse_word_list
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import silences as silence_cache
from animatter.models import Deletion as DeletionRow
from animatter.models import EditOp, Media, Sentence, Token, TranscriptVersion
from animatter.services import projects as project_service
from animatter.services import settings_store

log = get_logger(__name__)

#: The three kinds, in the order the dialog lists them.
KINDS: tuple[str, ...] = ("filler", "disfluency", "silence")

#: ``deletion.source`` values this module writes. They are how "restore fillers"
#: finds what to restore later, so they must stay stable.
_SOURCE_BY_KIND = {
    "filler": "filler",
    "disfluency": "disfluency",
    "silence": "silence",
}


@dataclass(frozen=True)
class PlanView:
    """Serialisable form of a plan, for the dialog."""

    version_id: str
    duration_ms: int
    items: list[dict[str, object]]
    summary: dict[str, dict[str, int]]
    fillers: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "duration_ms": self.duration_ms,
            "items": self.items,
            "summary": self.summary,
            "fillers": self.fillers,
            "kinds": list(KINDS),
        }


def _version_or_raise(session: Session, version_id: str) -> TranscriptVersion:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)
    return version


def _media_or_raise(session: Session, version: TranscriptVersion) -> Media:
    media = session.get(Media, version.media_id)
    if media is None:
        raise AppError("media_not_found", "找不到对应的音频", status=404)
    return media


def _deleted_token_ids(session: Session, version_id: str) -> set[int]:
    """Token indices already removed by any deletion row."""
    gone: set[int] = set()
    rows = session.scalars(
        select(DeletionRow).where(DeletionRow.version_id == version_id, DeletionRow.removed.is_(False))
    ).all()
    for row in rows:
        try:
            gone.update(int(value) for value in json.loads(row.token_ids_json or "[]"))
        except (ValueError, TypeError):
            continue
    return gone


def _hidden_token_ids(session: Session, version_id: str) -> set[int]:
    """Token indices already hidden from the text (batch or by hand).

    The filler finder skips these: a word the user hid on purpose must not come
    back as a proposal to *delete* its audio, which is the opposite of what they
    asked for.
    """
    rows = session.scalars(
        select(Token).where(Token.version_id == version_id, Token.hidden.is_(True))
    ).all()
    return {row.idx for row in rows}


def _tokens(session: Session, version_id: str) -> list[tuple[int, str, int, int]]:
    rows = session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx)).all()
    return [(row.idx, row.text, row.start_ms, row.end_ms) for row in rows]


def _sentence_of(session: Session, version_id: str) -> dict[int, int]:
    rows = session.scalars(
        select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord)
    ).all()
    mapping: dict[int, int] = {}
    for row in rows:
        for index in range(row.token_start, row.token_end):
            mapping[index] = row.ord
    return mapping


def load_silence_ranges(
    session: Session, settings: Settings, version: TranscriptVersion, media: Media
) -> list[TimeRange]:
    """Detected silence for this media, cached by content hash + parameters.

    The detector settings are the *silence map* ones (``silence_noise_db`` /
    ``silence_min_duration_ms``), deliberately separate from the cleanup cut
    threshold: the map also feeds boundary snapping, and those two decisions
    want different values.
    """
    stored = settings_store.load(session)
    noise_db = float(stored["silence_noise_db"])
    min_duration_ms = int(stored["silence_min_duration_ms"])
    project_dir = project_service.project_root(settings, version.project_id)

    cached = silence_cache.load_or_build(
        settings.ffmpeg_path,
        project_dir=project_dir,
        media_path=Path(media.path),
        media_sha=media.sha256,
        duration_ms=media.duration_ms,
        noise_db=noise_db,
        min_duration_s=min_duration_ms / 1000,
    )
    return [TimeRange(start, end) for start, end in cached.spans]


def plan(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    min_silence_ms: int | None = None,
    silence_pad_ms: int | None = None,
    min_disfluency_ms: int | None = None,
) -> PlanView:
    """What cleanup would remove right now. Read-only."""
    version = _version_or_raise(session, version_id)
    media = _media_or_raise(session, version)
    stored = settings_store.load(session)

    fillers = frozenset(parse_word_list(str(stored["cleanup_fillers"])))
    silences = load_silence_ranges(session, settings, version, media)

    result: CleanupPlan = build_plan(
        PlanRequest(
            tokens=_tokens(session, version_id),
            duration_ms=media.duration_ms,
            silences=silences,
            fillers=fillers,
            min_silence_ms=min_silence_ms or int(stored["cleanup_min_silence_ms"]),
            silence_pad_ms=(
                silence_pad_ms if silence_pad_ms is not None else int(stored["cleanup_silence_pad_ms"])
            ),
            min_disfluency_ms=min_disfluency_ms or int(stored["cleanup_min_disfluency_ms"]),
            sentence_of=_sentence_of(session, version_id),
            deleted=_deleted_token_ids(session, version_id),
            hidden=_hidden_token_ids(session, version_id),
        )
    )

    items = [
        {
            "kind": item.kind,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "duration_ms": item.duration_ms,
            "text": item.text,
            "token_indices": list(item.token_indices),
            "sentence_ord": item.sentence_ord,
        }
        for item in result.selected(set(KINDS))
    ]
    return PlanView(
        version_id=version_id,
        duration_ms=media.duration_ms,
        items=items,
        summary=result.summary(),
        fillers=sorted(fillers),
    )


def apply(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    kinds: set[str] | None = None,
    min_silence_ms: int | None = None,
    silence_pad_ms: int | None = None,
    min_disfluency_ms: int | None = None,
) -> dict[str, object]:
    """Recompute the plan and commit it as **one** undoable operation."""
    version = _version_or_raise(session, version_id)
    media = _media_or_raise(session, version)
    stored = settings_store.load(session)
    wanted = {kind for kind in (kinds or set(KINDS)) if kind in KINDS}
    if not wanted:
        raise AppError(
            "cleanup_nothing_selected",
            "没有勾选要清理的内容",
            hint="至少勾选「口癖词」「未转写语流」「静音」中的一项。",
        )

    fillers = frozenset(parse_word_list(str(stored["cleanup_fillers"])))
    silences = load_silence_ranges(session, settings, version, media)
    deleted = _deleted_token_ids(session, version_id)
    plan_result = build_plan(
        PlanRequest(
            tokens=_tokens(session, version_id),
            duration_ms=media.duration_ms,
            silences=silences,
            fillers=fillers,
            min_silence_ms=min_silence_ms or int(stored["cleanup_min_silence_ms"]),
            silence_pad_ms=(
                silence_pad_ms if silence_pad_ms is not None else int(stored["cleanup_silence_pad_ms"])
            ),
            min_disfluency_ms=min_disfluency_ms or int(stored["cleanup_min_disfluency_ms"]),
            sentence_of=_sentence_of(session, version_id),
            deleted=deleted,
            hidden=_hidden_token_ids(session, version_id),
        )
    )

    chosen = plan_result.selected(wanted)
    if not chosen:
        raise AppError(
            "cleanup_nothing_to_do",
            "这次没有可清理的内容",
            hint="换个阈值试试，或者词表里还没有能匹配到的口癖词。",
        )

    batch = new_id("cln")
    rows: list[DeletionRow] = []
    for item in chosen:
        # A word cut is addressed by token ids so "restore fillers" and undo can
        # map back to the transcript; an audio cut has none.
        token_ids = list(item.token_indices) if item.kind == "filler" else []
        if token_ids:
            first = session.scalars(
                select(Token).where(Token.version_id == version_id, Token.idx == token_ids[0])
            ).first()
            last = session.scalars(
                select(Token).where(Token.version_id == version_id, Token.idx == token_ids[-1])
            ).first()
            if first is None or last is None:
                continue
            start_ms, end_ms = first.start_ms, last.end_ms
        else:
            start_ms, end_ms = item.start_ms, item.end_ms
        if end_ms <= start_ms:
            continue
        row = DeletionRow(
            id=new_id("del"),
            version_id=version_id,
            start_ms=max(0, start_ms),
            end_ms=end_ms,
            source=_SOURCE_BY_KIND[item.kind],
            token_ids_json=json.dumps(token_ids),
            created_at=utcnow_iso(),
            removed=False,
            batch_id=batch,
        )
        session.add(row)
        rows.append(row)

    if not rows:
        raise AppError(
            "cleanup_nothing_to_do",
            "这次没有可清理的内容",
            hint="选中的片段都已经是删除状态了。",
        )

    payload = {
        "batch": batch,
        "kinds": sorted(wanted),
        "deletion_ids": [row.id for row in rows],
        "counts": _counts_by_source(rows),
    }
    op = EditOp(
        id=new_id("edt"),
        project_id=version.project_id,
        version_id=version_id,
        seq=_next_seq(session, version_id),
        op_type="cleanup",
        payload_json=json.dumps(payload, ensure_ascii=False),
        created_at=utcnow_iso(),
    )
    session.add(op)
    session.flush()

    log.info(
        "cleanup.applied",
        version=version_id,
        batch=batch,
        kinds=sorted(wanted),
        deletions=len(rows),
    )
    return {
        "batch": batch,
        "op_id": op.id,
        "deletions": len(rows),
        "counts": payload["counts"],
        "removed_ms": sum(row.end_ms - row.start_ms for row in rows),
        "kinds": sorted(wanted),
    }


def _counts_by_source(rows: list[DeletionRow]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(KINDS, 0)
    for row in rows:
        counts[row.source] = counts.get(row.source, 0) + 1
    return counts


def hide_fillers(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    min_silence_ms: int | None = None,
    silence_pad_ms: int | None = None,
    min_disfluency_ms: int | None = None,
) -> dict[str, object]:
    """Batch-hide the filler words instead of cutting them.

    The one-click counterpart of pressing Backspace on every «嗯». It exists
    because the honest answer to filler words is usually *not* to delete them:
    cutting 0.4 s out of the middle of a sentence leaves an audible hole, and
    what the user actually wanted was clean subtitles.

    Written as one ``hide`` edit op, so it is one line in the history and one
    undo — 400 words must not become 400 entries.

    Deliberately *not* a ``cleanup`` op: that type means "deletion rows were
    created", and undo is implemented as marking those rows removed. Reusing it
    here would make undo silently do nothing.
    """
    version = _version_or_raise(session, version_id)
    media = _media_or_raise(session, version)
    stored = settings_store.load(session)

    fillers = frozenset(parse_word_list(str(stored["cleanup_fillers"])))
    result = build_plan(
        PlanRequest(
            tokens=_tokens(session, version_id),
            duration_ms=media.duration_ms,
            silences=load_silence_ranges(session, settings, version, media),
            fillers=fillers,
            min_silence_ms=min_silence_ms or int(stored["cleanup_min_silence_ms"]),
            silence_pad_ms=(
                silence_pad_ms if silence_pad_ms is not None else int(stored["cleanup_silence_pad_ms"])
            ),
            min_disfluency_ms=min_disfluency_ms or int(stored["cleanup_min_disfluency_ms"]),
            sentence_of=_sentence_of(session, version_id),
            deleted=_deleted_token_ids(session, version_id),
            hidden=_hidden_token_ids(session, version_id),
        )
    )

    indices: list[int] = []
    for item in result.fillers:
        indices.extend(item.token_indices)
    indices.sort()
    if not indices:
        raise AppError(
            "cleanup_nothing_to_do",
            "没有可隐去的口癖词",
            hint="换个词表试试，或者这些词已经被隐去 / 删除了。",
        )

    tokens = {row.idx: row for row in session.scalars(
        select(Token).where(Token.version_id == version_id, Token.idx.in_(indices))
    ).all()}
    changed = [idx for idx in indices if idx in tokens]
    session.query(Token).filter(
        Token.version_id == version_id,
        Token.idx.in_(changed),
    ).update(
        # The source mark is what makes these eligible for "还原已隐去口癖词"
        # while leaving hand-hidden words alone.
        {Token.hidden: True, Token.hide_source: "filler"},
        synchronize_session=False,
    )
    session.flush()

    # Report in the units the dialog promised. It counts *places* — adjacent
    # fillers («嗯嗯») are one row, one grey band — so the answer must be the same
    # 173, not the 183 raw words. Two different numbers for one click reads as a
    # bug even when both are individually correct, and the duration differs too
    # (a merged band covers the gap between the two words).
    spans = len(result.fillers)
    span_ms = sum(item.duration_ms for item in result.fillers)
    op = EditOp(
        id=new_id("edt"),
        project_id=version.project_id,
        version_id=version_id,
        seq=_next_seq(session, version_id),
        op_type="hide",
        payload_json=json.dumps(
            {
                "token_start": changed[0],
                "token_end": changed[-1] + 1,
                "indices": changed,
                "hidden": True,
                "hide_source": "filler",
                "spans": spans,
                "text": "".join(tokens[idx].text for idx in changed),
                "batch": True,
            },
            ensure_ascii=False,
        ),
        created_at=utcnow_iso(),
    )
    session.add(op)
    session.flush()

    log.info("cleanup.hid_fillers", version=version_id, spans=spans, words=len(changed))
    return {
        "op_id": op.id,
        # ``hidden`` is in *places*, matching what the dialog counted and what the
        # grey bands on the waveform show. ``words`` is the raw token count, which
        # is larger whenever adjacent fillers merged.
        "hidden": spans,
        "words": len(changed),
        "span_ms": span_ms,
        "counts": {"filler": spans},
        "kinds": ["filler"],
    }


def _next_seq(session: Session, version_id: str) -> int:
    last = session.scalars(
        select(EditOp).where(EditOp.version_id == version_id).order_by(EditOp.seq.desc())
    ).first()
    return (last.seq if last is not None else 0) + 1


def _hidden_filler_runs(session: Session, version_id: str) -> list[list[Token]]:
    """Batch-hidden filler words, grouped into contiguous runs.

    A *run* is what the user sees as one grey band and what the dialog counted as
    one 「处」. Counting raw tokens instead made the confirmation read «已隐去 173
    处» and the menu immediately afterwards say «还原已隐去口癖词（183）» — two
    numbers for the same click, which reads as a bug even when both are right.
    """
    rows = list(
        session.scalars(
            select(Token)
            .where(
                Token.version_id == version_id,
                Token.hidden.is_(True),
                Token.hide_source == "filler",
            )
            .order_by(Token.idx)
        )
    )
    runs: list[list[Token]] = []
    for row in rows:
        if runs and row.idx == runs[-1][-1].idx + 1:
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def restore(
    session: Session,
    version_id: str,
    *,
    source: str | None = None,
    batch: str | None = None,
) -> dict[str, object]:
    """Undo a cleanup: by category (all fillers) or by batch (this one click).

    Both paths are soft deletes, so the audio is recoverable either way and the
    history stays honest about what happened.

    ``source='hidden_filler'`` is the other half of the same idea: it un-hides the
    words the batch cleanup hid, and **only** those. Hand-hidden words keep their
    ``hide_source=''`` mark and are restored one at a time from the history panel,
    because a bulk "take it back" must not discard decisions the user made while
    reading.
    """
    _version_or_raise(session, version_id)

    if source == "hidden_filler":
        runs = _hidden_filler_runs(session, version_id)
        for run in runs:
            for token in run:
                token.hidden = False
                token.hide_source = ""
        session.flush()
        # 「处」, matching the count that was shown when they were hidden.
        log.info("cleanup.restored_hidden", version=version_id, places=len(runs))
        return {"restored": len(runs), "source": source, "batch": None}

    stmt = select(DeletionRow).where(
        DeletionRow.version_id == version_id, DeletionRow.removed.is_(False)
    )
    if source is not None:
        if source not in KINDS:
            raise AppError("cleanup_bad_source", f"未知的清理类别：{source}")
        stmt = stmt.where(DeletionRow.source == source)
    if batch is not None:
        stmt = stmt.where(DeletionRow.batch_id == batch)
    if source is None and batch is None:
        raise AppError(
            "cleanup_nothing_specified",
            "没有指定要还原的清理",
            hint="按类别还原传给 source，按批次还原传给 batch。",
        )

    rows = list(session.scalars(stmt))
    for row in rows:
        row.removed = True
    session.flush()
    log.info("cleanup.restored", version=version_id, source=source, batch=batch, count=len(rows))
    return {"restored": len(rows), "source": source, "batch": batch}


def counts_by_kind(session: Session, version_id: str) -> dict[str, int]:
    """Currently-applied deletions per kind — what "还原已删除口癖词 (12)" shows.

    Plus ``hidden_filler``: how many words the batch cleanup hid. It is not a
    deletion count, but it is the same question from the menu's point of view —
    "is there anything of this kind to take back".
    """
    rows = session.scalars(
        select(DeletionRow).where(DeletionRow.version_id == version_id, DeletionRow.removed.is_(False))
    ).all()
    counts: dict[str, int] = dict.fromkeys(KINDS, 0)
    for row in rows:
        if row.source in counts:
            counts[row.source] += 1
    # In 「处」, not tokens: a run of adjacent fillers is one band on the waveform
    # and one row in the plan list. Anything else and the menu count disagrees
    # with the confirmation the user just read.
    counts["hidden_filler"] = len(_hidden_filler_runs(session, version_id))
    return counts


def available_restores(session: Session, version_id: str) -> dict[str, int]:
    """How many cleanups of each kind are currently in effect."""
    return counts_by_kind(session, version_id)
