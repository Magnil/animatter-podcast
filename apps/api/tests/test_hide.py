"""Hiding a word from the text without touching the audio.

The distinction this feature exists for: a deleted word loses its sound, a
hidden word keeps it. «嗯» and «那个» are the reason — cutting them out of the
render leaves audible gaps, but they should not appear in the published
subtitles. Getting the two confused would either destroy audio the user wanted or
leave filler words in the export.

:func:`tests.test_corrections.seed_version` makes **one token per character**, so
«嗯这个很好» is five tokens; the expectations below are written against that.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import editing, subtitles
from animatter.services import transcribe as transcribe_service


def hidden_flags(session: Session, version_id: str) -> list[bool]:
    return [token.hidden for token in transcribe_service.load_tokens(session, version_id)]


def test_hiding_marks_the_tokens(session: Session) -> None:
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    assert hidden_flags(session, version.id) == [True, False, False, False, False]


def test_hiding_does_not_move_any_timestamp(session: Session) -> None:
    """The whole point: the audio is untouched, so nothing on the timeline moves."""
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    before = [(t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version.id)]
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    after = [(t.start_ms, t.end_ms) for t in transcribe_service.load_tokens(session, version.id)]
    assert after == before


def test_hiding_does_not_shrink_the_token_stream(session: Session) -> None:
    """A hidden word still occupies its slot; only a correction/resize changes count."""
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    tokens = transcribe_service.load_tokens(session, version.id)
    assert len(tokens) == 5
    assert [t.text for t in tokens] == ["嗯", "这", "个", "很", "好"]


def test_can_un_hide(session: Session) -> None:
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    editing.apply_hide(session, version.id, token_start=0, token_end=1, hidden=False)
    assert hidden_flags(session, version.id) == [False] * 5


def test_hiding_twice_is_a_no_op(session: Session) -> None:
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    again = editing.apply_hide(session, version.id, token_start=0, token_end=1)
    # Nothing changed, so nothing was recorded: an undo of the second call would
    # otherwise un-hide a word the user hid in the first.
    assert again["changed"] == 0
    assert len(editing.list_ops(session, version.id)) == 1


def test_undo_restores_only_what_that_call_hid(session: Session) -> None:
    """Undo must be exact, not "un-hide everything in the recorded range"."""
    version = seed_version(session, text="嗯那个很好", sizes=[1, 1, 3])
    # Hide 这 and 个 in two separate operations.
    editing.apply_hide(session, version.id, token_start=1, token_end=2)
    editing.apply_hide(session, version.id, token_start=2, token_end=3)
    assert hidden_flags(session, version.id) == [False, True, True, False, False]

    latest = editing.list_ops(session, version.id)[0]
    editing.undo_op(session, version.id, latest.id)
    # Only the second call is undone; the first word stays hidden.
    assert hidden_flags(session, version.id) == [False, True, False, False, False]


def test_hide_appears_in_the_history_as_undoable(session: Session) -> None:
    version = seed_version(session, text="嗯这个很好", sizes=[1, 2, 2])
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    described = editing.describe_op(editing.list_ops(session, version.id)[0])
    assert described["op_type"] == "hide"
    assert described["label"] == "隐去文字"
    assert described["undoable"] is True
    assert "嗯" in str(described["detail"])


def test_hide_rejects_an_out_of_range_selection(session: Session) -> None:
    version = seed_version(session, text="嗯这", sizes=[1, 1])
    with pytest.raises(editing.EditError):
        editing.apply_hide(session, version.id, token_start=0, token_end=99)


def test_subtitle_text_omits_hidden_words_but_keeps_the_timing(session: Session) -> None:
    # One sentence, so the cue covers the whole line and the timing comparison is
    # meaningful — the cue must not shrink to just the surviving words.
    version = seed_version(session, text="嗯这个很好", sizes=[5])
    before, _ = subtitles.build_cues(session, version.id, time_map=None)
    editing.apply_hide(session, version.id, token_start=0, token_end=1)
    after, _ = subtitles.build_cues(session, version.id, time_map=None)

    assert before[0].text == "嗯这个很好"
    assert after[0].text == "这个很好"
    # The cue still covers the same stretch of audio: the «嗯» is audible.
    assert (after[0].start_ms, after[0].end_ms) == (before[0].start_ms, before[0].end_ms)


def test_hiding_every_word_in_a_sentence_drops_that_cue_only(session: Session) -> None:
    version = seed_version(session, text="嗯这", sizes=[2])
    editing.apply_hide(session, version.id, token_start=0, token_end=2)
    cues, _ = subtitles.build_cues(session, version.id, time_map=None)
    # No empty cue is emitted, and nothing is invented in its place.
    assert cues == []
