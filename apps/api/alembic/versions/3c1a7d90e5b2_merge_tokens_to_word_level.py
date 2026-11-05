"""Merge character-level tokens into word-level tokens

The editing unit moved from the character to the ASR word (AGENT.md §6.3), so
existing transcripts must be rewritten: characters that came from one ASR word
become one token covering that word's real span.

The grouping key is the **contiguous run of ``src_word_idx`` inside a sentence**,
not ``src_word_idx`` alone: the ASR numbers words from 0 within each sentence, so
the same index appears many times in one version and grouping globally would
merge unrelated words seconds apart.

Edits already applied are preserved: a token with ``src_word_idx == -1`` (or any
token that is not adjacent to its neighbour in both index and word id) stays its
own token, because its provenance is gone and re-splitting it would be a guess.

Revision ID: 3c1a7d90e5b2
Revises: 56597775cf9d
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import Any

import sqlalchemy as sa
from alembic import op

#: One row from an ``sa.text`` SELECT: a plain tuple to mypy, a ``Row`` at runtime.
Row = Any

revision: str = "3c1a7d90e5b2"
down_revision: str | None = "56597775cf9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_word_level(
    tokens: Sequence[Row], sentences: Sequence[Row]
) -> bool:
    """True when no adjacent pair inside a sentence shares a ``src_word_idx``."""
    by_sentence: dict[int, list[Row]] = {}
    for row in tokens:
        for ord_, start, end in sentences:
            if int(start) <= int(row[0]) < int(end):
                by_sentence.setdefault(int(ord_), []).append(row)
                break
    for ord_, _start, _end in sentences:
        rows = by_sentence.get(int(ord_), [])
        for previous, current in pairwise(rows):
            if int(previous[4]) >= 0 and int(previous[4]) == int(current[4]):
                return False
    return True


def _merge_version(bind: sa.engine.Connection, version_id: str) -> int:
    tokens = bind.execute(
        sa.text(
            "SELECT idx, start_ms, end_ms, text, src_word_idx, punc_after FROM token "
            "WHERE version_id = :v ORDER BY idx"
        ),
        {"v": version_id},
    ).all()
    if not tokens:
        return 0

    sentences = bind.execute(
        sa.text(
            "SELECT ord, token_start, token_end FROM sentence "
            "WHERE version_id = :v ORDER BY ord"
        ),
        {"v": version_id},
    ).all()

    # Idempotence guard. If no two adjacent tokens inside a sentence share a
    # ``src_word_idx`` there is nothing left to merge, so the version is already
    # word-level and must be left alone. Without this a second run appended a
    # duplicate copy of the whole transcript (the rebuild is additive).
    if _is_word_level(tokens, sentences):
        return len(tokens)

    # Rewrite sentence by sentence so runs never cross a sentence boundary.
    rebuilt: list[tuple[int, int, str, int, str]] = []
    new_ranges: list[tuple[int, int, int]] = []
    #: Which source token idx values have been folded into ``rebuilt``. Tracking
    #: this (rather than comparing counts) is essential: merging always *reduces*
    #: the token count, so a count-based "orphan" check fires on every version.
    consumed: set[int] = set()

    def push_run(run: list[Row]) -> tuple[int, int]:
        first, last = run[0], run[-1]
        start_index = len(rebuilt)
        rebuilt.append(
            (
                int(first[1]),
                int(last[2]),
                "".join(str(row[3]) for row in run),
                int(first[4]),
                str(last[5]),
            )
        )
        return start_index, len(rebuilt)

    for ord_, token_start, token_end in sentences:
        slice_rows = [row for row in tokens if int(token_start) <= int(row[0]) < int(token_end)]
        if not slice_rows:
            new_ranges.append((int(ord_), len(rebuilt), len(rebuilt)))
            continue

        begin = len(rebuilt)
        run: list[Row] = []
        for row in slice_rows:
            consumed.add(int(row[0]))
        previous_idx: int | None = None
        previous_word: int | None = None

        for row in slice_rows:
            word_idx = int(row[4])
            idx = int(row[0])
            same_run = (
                run
                and word_idx == previous_word
                and word_idx >= 0
                and previous_idx is not None
                and idx == previous_idx + 1
            )
            if same_run:
                run.append(row)
            else:
                if run:
                    push_run(run)
                run = [row]
            previous_idx, previous_word = idx, word_idx

        if run:
            push_run(run)
        new_ranges.append((int(ord_), begin, len(rebuilt)))

    # Tokens outside every sentence cannot happen (INV-1), but if they do they are
    # appended so no audio silently loses its transcript.
    orphans = [row for row in tokens if int(row[0]) not in consumed]
    if orphans:
        orphan_start = len(rebuilt)
        for row in orphans:
            rebuilt.append(
                (int(row[1]), int(row[2]), str(row[3]), int(row[4]), str(row[5]))
            )
        if new_ranges:
            ord_, start, _ = new_ranges[-1]
            new_ranges[-1] = (ord_, start, len(rebuilt))
        else:
            new_ranges.append((0, orphan_start, len(rebuilt)))

    bind.execute(sa.text("DELETE FROM token WHERE version_id = :v"), {"v": version_id})
    bind.execute(
        sa.text(
            "INSERT INTO token (version_id, idx, start_ms, end_ms, text, kind, "
            "src_word_idx, punc_after, derived) VALUES (:v, :i, :s, :e, :t, 'word', :w, :p, 0)"
        ),
        [
            {
                "v": version_id,
                "i": index,
                "s": start_ms,
                "e": end_ms,
                "t": text,
                "w": src_word_idx,
                "p": punc_after,
            }
            for index, (start_ms, end_ms, text, src_word_idx, punc_after) in enumerate(rebuilt)
        ],
    )

    for ord_, start, end in new_ranges:
        bind.execute(
            sa.text(
                "UPDATE sentence SET token_start = :s, token_end = :e "
                "WHERE version_id = :v AND ord = :o"
            ),
            {"s": start, "e": end, "v": version_id, "o": ord_},
        )

    bind.execute(
        sa.text("UPDATE transcript_version SET token_count = :c WHERE id = :v"),
        {"c": len(rebuilt), "v": version_id},
    )
    return len(rebuilt)


def upgrade() -> None:
    bind = op.get_bind()
    version_ids = [
        row[0] for row in bind.execute(sa.text("SELECT id FROM transcript_version")).all()
    ]
    for version_id in version_ids:
        _merge_version(bind, str(version_id))


def downgrade() -> None:
    # Re-splitting into characters would have to invent per-character timings,
    # which is exactly the interpolation this migration removed. Refuse instead of
    # producing timestamps nobody observed.
    raise NotImplementedError(
        "无法降级：字级时间戳是插值出来的，回退会伪造从未观测过的时间"
    )
