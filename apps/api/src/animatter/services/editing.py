"""Transcript editing: sentence boundaries, text repair, and undo.

Two kinds of edit are recorded separately on purpose (AGENT.md §6.4):

* ``boundary`` — re-drawing sentence ranges; does not touch any token text;
* ``text`` — correcting a wrong character; does not touch any range.

Keeping them apart is what makes "which error does this show have most of"
answerable later, and it is what lets undo replay the right inverse.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.core.realign import (
    RealignError,
    TokenRange,
    adjust_token_edge,
    apply_replacement,
    locate_sentence,
)
from animatter.core.sentence import (
    InvalidBoundaryError,
    SentenceSpan,
    extend_into,
    merge_range,
    split_at,
    validate,
)
from animatter.core.tokens import TokenSpan
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import EditOp, Sentence, Token, TranscriptVersion
from animatter.services import transcribe as transcribe_service

log = get_logger(__name__)


class EditError(AppError):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(code, message, hint=hint)


def _version_or_raise(session: Session, version_id: str) -> TranscriptVersion:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise EditError("version_not_found", "转写版本不存在", "刷新版本列表。")
    return version


def _next_seq(session: Session, version_id: str) -> int:
    last = session.scalars(
        select(EditOp.seq).where(EditOp.version_id == version_id).order_by(EditOp.seq.desc())
    ).first()
    return (last or 0) + 1


def _record(
    session: Session,
    version: TranscriptVersion,
    *,
    op_type: str,
    payload: dict[str, object],
) -> EditOp:
    op = EditOp(
        id=new_id("edt"),
        project_id=version.project_id,
        version_id=version.id,
        seq=_next_seq(session, version.id),
        op_type=op_type,
        payload_json=json.dumps(payload, ensure_ascii=False),
        created_at=utcnow_iso(),
    )
    session.add(op)
    return op


# --- sentence boundaries ------------------------------------------------


def apply_boundary(
    session: Session,
    version_id: str,
    *,
    action: str,
    from_token: int,
    to_token: int,
    direction: str | None = None,
) -> list[SentenceSpan]:
    """Re-draw sentence boundaries.

    ``action`` is one of:

    * ``new_sentence`` — split at both edges of the selection;
    * ``to_prev`` / ``to_next`` — move the selection into the neighbour;
    * ``merge`` — collapse the selection into a single sentence.
    """
    version = _version_or_raise(session, version_id)
    tokens = transcribe_service.load_tokens(session, version_id)
    spans = transcribe_service.load_sentences(session, version_id)
    if not spans:
        raise EditError("no_sentences", "该版本还没有句子", "先完成转写。")

    before = [(s.token_start, s.token_end) for s in spans]

    try:
        if action == "new_sentence":
            spans = _split_around(spans, from_token, to_token)
        elif action == "merge":
            spans = merge_range(spans, from_token, to_token)
        elif action in {"to_prev", "to_next"}:
            spans = extend_into(
                spans,
                from_token,
                to_token,
                direction="prev" if action == "to_prev" else "next",
            )
        else:
            raise EditError("unknown_action", f"未知的边界操作 {action}")
        validate(spans, len(tokens))
    except InvalidBoundaryError as exc:
        raise EditError("invalid_boundary", str(exc), "检查选区是否跨句或落在边界上。") from exc

    _persist_spans(session, version_id, spans)
    _record(
        session,
        version,
        op_type="boundary",
        payload={
            "action": action,
            "direction": direction,
            "from_token": from_token,
            "to_token": to_token,
            "before": before,
            "after": [[s.token_start, s.token_end] for s in spans],
        },
    )
    log.info("edit.boundary", action=action, from_token=from_token, to_token=to_token)
    return spans


def _split_around(spans: list[SentenceSpan], from_token: int, to_token: int) -> list[SentenceSpan]:
    """Split at both edges so the selection becomes its own sentence."""
    result = spans
    # Cut the far edge first: splitting changes no indices, only ranges, so the
    # earlier index stays valid.
    if to_token > 0 and to_token < result[-1].token_end and not _is_boundary(result, to_token):
        result = split_at(result, to_token)
    if from_token > 0 and not _is_boundary(result, from_token):
        result = split_at(result, from_token)
    return result


def _is_boundary(spans: list[SentenceSpan], token_index: int) -> bool:
    return any(span.token_start == token_index for span in spans)


def _persist_spans(session: Session, version_id: str, spans: list[SentenceSpan]) -> None:
    """Replace the sentence rows for a version with ``spans``."""
    existing = {
        row.ord: row for row in session.scalars(select(Sentence).where(Sentence.version_id == version_id))
    }
    now = utcnow_iso()

    for span in spans:
        row = existing.get(span.ord)
        if row is None:
            session.add(
                Sentence(
                    id=new_id("sen"),
                    version_id=version_id,
                    ord=span.ord,
                    token_start=span.token_start,
                    token_end=span.token_end,
                    speaker_id=span.speaker_id,
                    origin=span.origin,
                    updated_at=now,
                )
            )
        else:
            row.token_start = span.token_start
            row.token_end = span.token_end
            row.origin = span.origin
            row.updated_at = now

    for ord_, row in existing.items():
        if ord_ >= len(spans):
            session.delete(row)

    # The version's counters are a denormalised cache of the sentence rows: the
    # editor and the archive both read them, and boundary merges/splits change
    # the count without touching the tokens. Leaving them stale is how a 5-sentence
    # transcript ends up claiming "3 句" in the header.
    version = session.get(TranscriptVersion, version_id)
    if version is not None:
        version.sentence_count = len(spans)

    session.flush()


# --- text repair --------------------------------------------------------


def apply_text_edit(
    session: Session,
    version_id: str,
    *,
    proposed: str,
    sentence_ord: int | None = None,
    original: str | None = None,
    token_start: int | None = None,
    token_end: int | None = None,
) -> dict[str, object]:
    """Replace a span of text with ``proposed``, never moving the timeline.

    Two addressing modes, because the UI has two natural entry points:

    * ``token_start``/``token_end`` — an explicit character selection. This is
      what the editor sends, and it is the only mode that can express a
      replacement whose original text spans punctuation.
    * ``sentence_ord`` + ``original`` — locate the text inside one sentence.

    Both resolve to a token range; ``apply_replacement`` guarantees the range's
    millisecond boundaries survive unchanged.
    """
    version = _version_or_raise(session, version_id)
    spans = transcribe_service.load_sentences(session, version_id)
    tokens = transcribe_service.load_tokens(session, version_id)

    if token_start is not None and token_end is not None:
        if not 0 <= token_start < token_end <= len(tokens):
            raise EditError(
                "token_range_invalid",
                f"选区 {token_start}–{token_end} 超出范围（共 {len(tokens)} 个 token）",
                "重新选择要修改的文字。",
            )
        token_range = TokenRange(token_start, token_end)
        original = "".join(t.text for t in tokens[token_start:token_end])
        resolved_ord = next(
            (s.ord for s in spans if token_start >= s.token_start and token_end <= s.token_end),
            -1,
        )
        if resolved_ord < 0:
            # Cross-sentence replacement is legal (punctuation often sits at a
            # boundary), but record the first sentence it touches.
            resolved_ord = next(
                (s.ord for s in spans if token_start < s.token_end and token_end > s.token_start),
                -1,
            )
        sentence_ord = resolved_ord
    else:
        if sentence_ord is None or original is None:
            raise EditError(
                "edit_target_missing",
                "没有指定要修改的位置",
                "提供 token 区间，或提供句子编号与原文。",
            )
        span = next((s for s in spans if s.ord == sentence_ord), None)
        if span is None:
            raise EditError("sentence_not_found", f"句子 {sentence_ord} 不存在")

        located = locate_sentence(tokens, span.token_start, span.token_end, original)
        if located is None:
            current = "".join(t.text + t.punc_after for t in tokens[span.token_start : span.token_end])
            raise EditError(
                "text_not_found",
                f"在该句中找不到「{original}」",
                f"当前句子是「{current}」，请精确选择要替换的片段。",
            )
        token_range = TokenRange(located.start, located.end)

    try:
        result = apply_replacement(tokens, token_range, proposed)
    except RealignError as exc:
        raise EditError("realign_failed", str(exc), "该位置无法在不移动时间轴的前提下改写。") from exc

    _replace_token_window(session, version, token_range, result.tokens, result.token_delta)
    _record(
        session,
        version,
        op_type="text",
        payload={
            "sentence_ord": sentence_ord,
            "token_start": token_range.start,
            "original": original,
            "proposed": proposed,
            "delta": result.token_delta,
        },
    )
    return {
        "sentence_ord": sentence_ord,
        "token_start": token_range.start,
        "token_end": token_range.start + max(1, len(proposed)),
        "delta": result.token_delta,
    }


def _replace_token_window(
    session: Session,
    version: TranscriptVersion,
    token_range: TokenRange,
    rewritten: list[TokenSpan],
    delta: int,
) -> None:
    """Swap one window of tokens for ``rewritten``, keeping idx dense."""
    session.query(Token).filter(
        Token.version_id == version.id,
        Token.idx >= token_range.start,
        Token.idx < token_range.end,
    ).delete(synchronize_session=False)
    session.flush()

    if delta != 0:
        tail = list(
            session.scalars(
                select(Token)
                .where(Token.version_id == version.id, Token.idx >= token_range.end)
                .order_by(Token.idx)
            )
        )
        # Shift from the end so intermediate collisions cannot happen.
        for row in reversed(tail):
            row.idx += delta
        session.flush()

    new_count = token_range.length + delta
    for token in rewritten[token_range.start : token_range.start + new_count]:
        session.add(
            Token(
                version_id=version.id,
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

    if delta != 0:
        _shift_sentences(session, version.id, at_token=token_range.start, delta=delta)
        # ``token_count`` is read by the header, the archive and the subtitle
        # panel; a text edit silently changes it.
        version.token_count += delta
    session.flush()


def _shift_sentences(session: Session, version_id: str, *, at_token: int, delta: int) -> None:
    from animatter.core.realign import shift_ranges_after

    rows = list(
        session.scalars(select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord))
    )
    ranges = shift_ranges_after([(r.token_start, r.token_end) for r in rows], at_token=at_token, delta=delta)
    for row, (start, end) in zip(rows, ranges, strict=False):
        row.token_start = start
        row.token_end = end
        row.updated_at = utcnow_iso()
    session.flush()


# --- boundary dragging --------------------------------------------------


def adjust_boundary_edge(
    session: Session,
    version_id: str,
    *,
    token_index: int,
    edge: str,
    new_ms: int,
) -> dict[str, object]:
    """Move one token edge — the waveform drag operation.

    Semantics the UI relies on:

    * Dragging the **first** token's start moves the sentence's start.
    * Dragging the **last** token's end moves the sentence's end.
    * Dragging anything in between only changes that one token.

    In all cases every *other* token keeps its exact timing, so correcting a
    sloppy ASR boundary does not silently shift the words inside the sentence.
    """
    version = _version_or_raise(session, version_id)
    tokens = transcribe_service.load_tokens(session, version_id)

    previous = tokens[token_index] if 0 <= token_index < len(tokens) else None
    if previous is None:
        raise EditError("token_not_found", f"token {token_index} 不存在", "刷新转录后重试。")

    try:
        updated, outcome = adjust_token_edge(tokens, token_index, edge, new_ms)
    except RealignError as exc:
        raise EditError("edge_adjust_failed", str(exc)) from exc

    if updated == previous:
        return {
            "token_index": token_index,
            "edge": edge,
            "start_ms": previous.start_ms,
            "end_ms": previous.end_ms,
            "clamped": outcome.clamped,
            "reason": outcome.reason,
            "changed": False,
        }

    row = session.scalars(
        select(Token).where(Token.version_id == version.id, Token.idx == token_index)
    ).first()
    if row is None:
        raise EditError("token_not_found", f"token {token_index} 不存在")

    row.start_ms = updated.start_ms
    row.end_ms = updated.end_ms
    row.derived = False  # a human moved it; it is no longer interpolated

    _record(
        session,
        version,
        op_type="boundary_time",
        payload={
            "token_index": token_index,
            "edge": edge,
            "before": [previous.start_ms, previous.end_ms],
            "after": [updated.start_ms, updated.end_ms],
        },
    )
    session.flush()
    log.info("edit.edge", token=token_index, edge=edge, to=new_ms, clamped=outcome.clamped)

    return {
        "token_index": token_index,
        "edge": edge,
        "start_ms": updated.start_ms,
        "end_ms": updated.end_ms,
        "clamped": outcome.clamped,
        "reason": outcome.reason,
        "changed": True,
    }


# --- history ------------------------------------------------------------


def list_ops(session: Session, version_id: str) -> list[EditOp]:
    return list(
        session.scalars(select(EditOp).where(EditOp.version_id == version_id).order_by(EditOp.seq.desc()))
    )


#: Human-readable labels so the history panel does not show raw op codes.
_OP_LABELS: dict[str, str] = {
    "boundary": "调整句边界",
    "text": "修改文字",
    "boundary_time": "拖动边界时间",
    "retoken_text": "应用校对修正",
    "cleanup": "一键清理",
}

#: Human-facing names for the cleanup categories, shared with the dialog.
_CLEANUP_KINDS: dict[str, str] = {
    "filler": "口癖词",
    "disfluency": "未转写语流",
    "silence": "静音",
}

_BOUNDARY_ACTIONS: dict[str, str] = {
    "new_sentence": "另起一句",
    "to_prev": "并入上句",
    "to_next": "并入下句",
    "merge": "合并句子",
}


def describe_op(op: EditOp) -> dict[str, object]:
    """Render one history entry for the operations panel."""
    payload: dict[str, object] = {}
    try:
        payload = json.loads(op.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}

    label = _OP_LABELS.get(op.op_type, op.op_type)
    detail = ""
    if op.op_type == "boundary":
        detail = _BOUNDARY_ACTIONS.get(str(payload.get("action", "")), "")
    elif op.op_type == "text" or op.op_type == "retoken_text":
        detail = f"「{payload.get('original', '')}」→「{payload.get('proposed', '')}」"
    elif op.op_type == "boundary_time":
        before = payload.get("before") or [0, 0]
        after = payload.get("after") or [0, 0]
        detail = f"第 {payload.get('token_index', '?')} 字 {before} → {after}"
    elif op.op_type == "cleanup":
        counts = payload.get("counts")
        rendered = counts if isinstance(counts, dict) else {}
        parts = [
            f"{_CLEANUP_KINDS.get(str(kind), str(kind))} {count}"
            for kind, count in rendered.items()
            if count
        ]
        detail = " · ".join(parts) if parts else f"{payload.get('deletions', 0)} 处"

    return {
        "id": op.id,
        "seq": op.seq,
        "op_type": op.op_type,
        "label": label,
        "detail": detail,
        "created_at": op.created_at,
        "undone": op.undone,
        # Boundary ops restore a stored snapshot; cleanup ops flip a known set
        # of deletion rows back on. Both are exact, so both are undoable.
        "undoable": op.op_type in {"boundary", "cleanup"} and not op.undone,
    }


def undo_op(session: Session, version_id: str, op_id: str | None = None) -> dict[str, object]:
    """Undo a specific boundary edit, or the latest one when ``op_id`` is omitted.

    Reverting an *earlier* operation than the last one is deliberately refused:
    later edits may have shifted token indices, so replaying an old snapshot would
    silently discard or corrupt them.
    """
    _version_or_raise(session, version_id)

    if op_id is None:
        op = session.scalars(
            select(EditOp)
            .where(EditOp.version_id == version_id, EditOp.undone.is_(False))
            .order_by(EditOp.seq.desc())
        ).first()
    else:
        op = session.get(EditOp, op_id)
        if op is not None and op.version_id != version_id:
            op = None

    if op is None:
        raise EditError("nothing_to_undo", "没有可撤销的操作")
    if op.undone:
        raise EditError("already_undone", "该操作已经撤销过了")
    if op.op_type not in {"boundary", "cleanup"}:
        raise EditError(
            "undo_unsupported",
            f"「{_OP_LABELS.get(op.op_type, op.op_type)}」不支持直接撤销",
            "文字修改请用反向替换恢复原文；删除请用「恢复删除」。",
        )

    latest = session.scalars(
        select(EditOp)
        .where(EditOp.version_id == version_id, EditOp.undone.is_(False))
        .order_by(EditOp.seq.desc())
    ).first()
    if latest is not None and latest.id != op.id:
        raise EditError(
            "undo_out_of_order",
            "只能撤销最近一次边界操作",
            "更早的操作之后还有别的编辑，回滚它会覆盖掉那些改动。",
        )

    payload = json.loads(op.payload_json)

    if op.op_type == "cleanup":
        from animatter.models import Deletion as DeletionRow

        ids = [str(value) for value in payload.get("deletion_ids") or []]
        rows = session.scalars(
            select(DeletionRow).where(
                DeletionRow.version_id == version_id,
                DeletionRow.id.in_(ids),
                DeletionRow.removed.is_(False),
            )
        ).all()
        for row in rows:
            row.removed = True
        op.undone = True
        session.flush()
        log.info("edit.undo_cleanup", op_id=op.id, restored=len(rows))
        return {"undone": op.id, "op_type": op.op_type}

    before = payload.get("before") or []
    spans = [SentenceSpan(ord=i, token_start=int(s), token_end=int(e)) for i, (s, e) in enumerate(before)]
    tokens = transcribe_service.load_tokens(session, version_id)
    validate(spans, len(tokens))
    _persist_spans(session, version_id, spans)
    op.undone = True
    session.flush()
    log.info("edit.undo", op_id=op.id, action=payload.get("action"))
    return {"undone": op.id, "op_type": op.op_type}


def undo_last(session: Session, version_id: str) -> dict[str, object]:
    return undo_op(session, version_id)
