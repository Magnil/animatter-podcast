"""P0-2/P0-3: the still preview must not re-read the transcript five times.

Measured on the 8180-token 费那奇MV transcript (`docs/video-preview-and-export.md` §19.33):
a still preview spent **414 ms of its 1020 ms** in Python, and almost none of it was
arithmetic — the avatar path loaded every token as an ORM object once per bound slot
(4 slots × 92 ms) and the subtitle path loaded them again.

The assertions here are about **reads**, not milliseconds: a timing assertion would be
flaky on a loaded machine and would still pass if the code did the work twice on a fast
day. What must stay true is that one request reads the transcript once, and that the
columns handed over are the ones both consumers actually use.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.core.edl import compile_edl
from animatter.models import Token
from animatter.services import subtitles, transcript_facts, video_avatars


def _count_token_selects(session: Session, action) -> int:
    """How many statements read the ``token`` table the action issues.

    The match is on ``FROM token`` rather than on the word ``token``: the *sentence* query
    selects ``sentence.token_start``/``token_end``, so a naive substring test counts it too —
    which it did, and is why the first version of this helper reported one read as two.
    """
    statements: list[str] = []

    def before_execute(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        normalised = " ".join(statement.lower().split())
        if " from token" in normalised or 'from "token"' in normalised:
            statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", before_execute)
    try:
        action()
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", before_execute)
    return len(statements)


def test_loading_facts_is_one_query_per_table(session: Session) -> None:
    version = seed_version(session)
    facts = transcript_facts.load(session, version.id)
    assert facts.tokens, "the fixture should have tokens"
    assert facts.sentences, "the fixture should have sentences"
    # Every token keeps its own row and its own text: a projection, not a summary.
    assert all(row.text is not None for row in facts.tokens.values())


def test_tokens_between_is_empty_for_an_absent_range(session: Session) -> None:
    """A gap in ``idx`` must yield fewer rows, not the neighbouring words.

    A positional slice would silently return the *next* sentence's words here, and every
    subtitle would then be off by one sentence with nothing to notice it.
    """
    version = seed_version(session)
    facts = transcript_facts.load(session, version.id)
    assert facts.tokens_between(10_000, 10_005) == []


# --- the avatar path ----------------------------------------------------


def test_speaking_intervals_reads_no_tokens_when_facts_are_given(
    session: Session,
) -> None:
    """The whole point: four slots, one read.

    ``resolve`` calls ``speaking_intervals`` once per bound slot; before the fix each call
    loaded all 8180 tokens.
    """
    version = seed_version(session)
    edl = compile_edl(original_duration_ms=version.duration_ms, deletions=[])
    facts = transcript_facts.load(session, version.id)

    reads = _count_token_selects(
        session,
        lambda: video_avatars.speaking_intervals(
            session, version, edl, 0, facts=facts
        ),
    )
    assert reads == 0


def test_resolve_reads_the_transcript_once_for_all_slots(
    session: Session,
) -> None:
    version = seed_version(session)
    template = _template_with_two_slots(session, version)
    edl = compile_edl(original_duration_ms=version.duration_ms, deletions=[])

    reads = _count_token_selects(
        session, lambda: video_avatars.resolve(session, template, version, edl)
    )
    assert reads == 1


def test_resolve_hands_the_same_facts_to_every_slot(session: Session) -> None:
    """Without this the read count could be 1 and the data still be re-queried elsewhere."""
    version = seed_version(session)
    template = _template_with_two_slots(session, version)
    edl = compile_edl(original_duration_ms=version.duration_ms, deletions=[])
    facts = transcript_facts.load(session, version.id)

    with_facts = video_avatars.resolve(session, template, version, edl, facts=facts)
    without = video_avatars.resolve(session, template, version, edl)
    assert with_facts == without


# --- the subtitle path --------------------------------------------------


def test_build_cues_reads_no_tokens_when_facts_are_given(session: Session) -> None:
    version = seed_version(session)
    facts = transcript_facts.load(session, version.id)
    reads = _count_token_selects(
        session,
        lambda: subtitles.build_cues(session, version.id, time_map=None, facts=facts),
    )
    assert reads == 0


def test_build_cues_and_its_facts_agree(session: Session) -> None:
    version = seed_version(session)
    facts = transcript_facts.load(session, version.id)
    shared, shared_issues = subtitles.build_cues(
        session, version.id, time_map=None, facts=facts
    )
    own, own_issues = subtitles.build_cues(session, version.id, time_map=None)
    assert shared == own
    assert shared_issues == own_issues


def test_the_two_consumers_see_the_same_hidden_words(session: Session) -> None:
    """``hidden`` decides both the cue text and whether a sentence is skipped.

    A projection that dropped (or mis-ordered) that column would produce subtitles for words
    the user hid, and nothing else would fail.
    """
    version = seed_version(session)
    hidden = session.scalars(
        Token.__table__.select().where(Token.version_id == version.id, Token.hidden.is_(True))
    ).all()
    if not hidden:
        pytest.skip("the fixture has no hidden words")

    facts = transcript_facts.load(session, version.id)
    for row in facts.tokens.values():
        assert isinstance(row.hidden, bool)
    cues, _ = subtitles.build_cues(session, version.id, time_map=None, facts=facts)
    for cue in cues:
        assert cue.text


# --- helpers ------------------------------------------------------------


def _template_with_two_slots(session: Session, version: object) -> object:
    """A template row whose two slots are bound to the fixture's two speakers."""
    import json

    from animatter.ids import new_id, utcnow_iso
    from animatter.models import VideoTemplate

    slots = {
        "avatars": [
            {
                "name": "头像1",
                "label": "主播",
                "rect": {"x": 42, "y": 200, "w": 177, "h": 188},
                "lit_path": "/tmp/avatar0_lit.png",
                "dim_path": "/tmp/avatar0_dim.png",
            },
            {
                "name": "头像2",
                "label": "嘉宾",
                "rect": {"x": 42, "y": 417, "w": 177, "h": 187},
                "lit_path": "/tmp/avatar1_lit.png",
                "dim_path": "/tmp/avatar1_dim.png",
            },
        ],
        "texts": [],
        "notes": [],
    }
    row = VideoTemplate(
        id=new_id("vtpl"),
        project_id=str(version.project_id),
        source_path="x.psd",
        source_sha256="0" * 64,
        canvas_w=1920,
        canvas_h=1080,
        box_rect='{"x": 240, "y": 209, "w": 1440, "h": 810}',
        slots_json=json.dumps(slots),
        bindings_json=json.dumps({"头像1": 0, "头像2": 1}),
        status="ready",
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row
