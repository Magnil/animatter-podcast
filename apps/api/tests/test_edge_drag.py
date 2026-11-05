"""Dragging a token edge on the waveform.

The contract users depend on: fixing one boundary must not nudge the words
inside the sentence. ASR timing is often a few hundred ms off at sentence edges,
and the fix has to be surgical.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from animatter.core.realign import MIN_TOKEN_MS, adjust_token_edge, clamp_edge
from animatter.core.tokens import TokenSpan
from animatter.models import Token


def toks(*specs: tuple[str, int, int]) -> list[TokenSpan]:
    return [
        TokenSpan(
            idx=i, start_ms=s, end_ms=e, text=t, kind="char", src_word_idx=0, punc_after="", derived=True
        )
        for i, (t, s, e) in enumerate(specs)
    ]


# --- clamping -----------------------------------------------------------


def test_clamp_within_range_is_noop() -> None:
    outcome = clamp_edge(500, 100, 900)
    assert outcome.applied_ms == 500
    assert outcome.clamped is False


def test_clamp_below_range_reports_reason() -> None:
    outcome = clamp_edge(50, 100, 900)
    assert outcome.applied_ms == 100
    assert outcome.clamped is True
    assert "重叠" in outcome.reason


def test_clamp_above_range() -> None:
    outcome = clamp_edge(999, 100, 900)
    assert outcome.applied_ms == 900
    assert outcome.clamped is True


def test_clamp_tolerates_inverted_bounds() -> None:
    outcome = clamp_edge(500, 900, 100)
    assert 100 <= outcome.applied_ms <= 900


# --- start edge ---------------------------------------------------------


def test_move_start_forward() -> None:
    tokens = toks(("一", 1000, 1500), ("二", 1500, 2000))
    updated, outcome = adjust_token_edge(tokens, 0, "start", 1200)
    assert (updated.start_ms, updated.end_ms) == (1200, 1500)
    assert outcome.clamped is False


def test_move_start_backward_into_silence() -> None:
    # Real ASR output leaves a gap between sentences; the drag may consume it.
    tokens = toks(("一", 1000, 1500), ("二", 2000, 2400))
    updated, outcome = adjust_token_edge(tokens, 1, "start", 1700)
    assert updated.start_ms == 1700
    assert outcome.clamped is False


def test_move_start_blocked_by_adjacent_token() -> None:
    # No gap: there is nothing to drag into, and the clamp must say why.
    tokens = toks(("一", 1000, 1500), ("二", 1500, 2000))
    updated, outcome = adjust_token_edge(tokens, 1, "start", 800)
    assert updated.start_ms == 1500
    assert outcome.clamped is True


def test_move_start_cannot_swallow_token() -> None:
    tokens = toks(("一", 1000, 1500))
    updated, outcome = adjust_token_edge(tokens, 0, "start", 1600)
    assert updated.end_ms - updated.start_ms >= MIN_TOKEN_MS
    assert outcome.clamped is True


def test_only_the_addressed_token_changes_on_start() -> None:
    original = toks(("一", 0, 400), ("二", 400, 800), ("三", 800, 1200))
    updated, _ = adjust_token_edge(original, 1, "start", 500)

    # Nothing else moved — this is the whole point of the operation.
    assert (original[0].start_ms, original[0].end_ms) == (0, 400)
    assert (updated.start_ms, updated.end_ms) == (500, 800)
    assert (original[2].start_ms, original[2].end_ms) == (800, 1200)


# --- end edge -----------------------------------------------------------


def test_move_end_forward() -> None:
    # Extending into the following silence is the common fix for clipped tails.
    tokens = toks(("一", 0, 400), ("二", 800, 1200))
    updated, outcome = adjust_token_edge(tokens, 0, "end", 600)
    assert (updated.start_ms, updated.end_ms) == (0, 600)
    assert outcome.clamped is False


def test_move_end_cannot_overlap_next() -> None:
    tokens = toks(("一", 0, 400), ("二", 400, 800))
    updated, outcome = adjust_token_edge(tokens, 0, "end", 700)
    assert updated.end_ms == 400
    assert outcome.clamped is True


def test_move_end_backward_keeps_minimum_duration() -> None:
    tokens = toks(("一", 1000, 1400))
    updated, outcome = adjust_token_edge(tokens, 0, "end", 1000)
    assert updated.end_ms >= updated.start_ms + MIN_TOKEN_MS
    assert outcome.clamped is True


# --- guard rails --------------------------------------------------------


def test_unknown_edge_rejected() -> None:
    from animatter.core.realign import RealignError

    with pytest.raises(RealignError, match="未知的边界方向"):
        adjust_token_edge(toks(("一", 0, 400)), 0, "middle", 200)


def test_out_of_range_index_rejected() -> None:
    from animatter.core.realign import RealignError

    with pytest.raises(RealignError, match="超出范围"):
        adjust_token_edge(toks(("一", 0, 400)), 5, "start", 100)


def test_noop_returns_same_token() -> None:
    tokens = toks(("一", 1000, 1400))
    updated, outcome = adjust_token_edge(tokens, 0, "start", 1000)
    assert updated == tokens[0]
    assert outcome.clamped is False


# --- service integration ------------------------------------------------


@pytest.fixture()
def session(client):  # type: ignore[no-untyped-def]
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def test_service_updates_only_target_token(session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    version = seed_version(session, text="一二三四", sizes=[2, 2])
    before = {t.idx: (t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version.id)}

    result = editing.adjust_boundary_edge(session, version.id, token_index=2, edge="start", new_ms=250)

    after = {t.idx: (t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version.id)}
    assert result["changed"] is True
    # Exactly one token differs.
    diffs = [idx for idx in before if before[idx] != after[idx]]
    assert diffs == [2]
    assert after[2] == (250, 300)


def test_service_marks_token_as_no_longer_derived(session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    # seed_version lays tokens end-to-end, so no edge can move. Widen the gap
    # first: a real recording always has silence between words.
    version = seed_version(session, text="一二三", sizes=[3])
    rows = list(session.scalars(select(Token).where(Token.version_id == version.id)))
    for row in rows[1:]:
        row.start_ms += 200
        row.end_ms += 200
    session.flush()

    result = editing.adjust_boundary_edge(session, version.id, token_index=0, edge="end", new_ms=250)
    assert result["changed"] is True

    tokens = transcribe_service.load_tokens(session, version.id)
    assert tokens[0].derived is False  # a human set this value


def test_service_records_history(session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三", sizes=[3])
    editing.adjust_boundary_edge(session, version.id, token_index=1, edge="start", new_ms=120)
    ops = editing.list_ops(session, version.id)
    assert any(op.op_type == "boundary_time" for op in ops)


def test_service_reports_clamp(session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    from animatter.services import editing
    from animatter.services import transcribe as transcribe_service

    # First token starts at 100 so pulling it below zero is a real clamp.
    version = seed_version(session, text="一二", sizes=[2])
    rows = list(session.scalars(select(Token).where(Token.version_id == version.id)))
    for row in rows:
        row.start_ms += 100
        row.end_ms += 100
    session.flush()

    result = editing.adjust_boundary_edge(session, version.id, token_index=0, edge="start", new_ms=-500)
    assert result["clamped"] is True
    assert result["start_ms"] == 0
    assert transcribe_service.load_tokens(session, version.id)[0].start_ms == 0
