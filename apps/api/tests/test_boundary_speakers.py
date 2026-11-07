"""Speaker annotations survive boundary edits.

The bug this file pins: `_persist_spans` matched sentence rows by `ord`, which is
*positional* — after a split, every sentence below the cut has a new ord. The
update path rewrote the range but never rewrote `speaker_id`, so each row kept
whatever label the previous occupant of that position had. Splitting 「A 说话」
into two made the second half inherit the *next* sentence's speaker; merging two
sentences silently dropped the label of the row that was deleted.

Every test below fails against the old code, and they are written at the service
boundary rather than the pure functions because that is where the persistence
step lives — the range algebra was right all along.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.services import editing
from animatter.services import transcribe as transcribe_service


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def annotate(session: Session, version_id: str, speakers: list[int | None]) -> None:
    """Set sentence speakers by ord, the way the speaker endpoint does."""
    sentences = transcribe_service.load_sentences(session, version_id)
    assert len(sentences) == len(speakers)
    from animatter.models import Sentence

    rows = {row.ord: row for row in session.query(Sentence).filter_by(version_id=version_id)}
    for ord_, speaker in enumerate(speakers):
        rows[ord_].speaker_id = speaker
    session.flush()


def speakers_of(session: Session, version_id: str) -> list[int | None]:
    return [span.speaker_id for span in transcribe_service.load_sentences(session, version_id)]


# --- the original bug ---------------------------------------------------


def test_split_does_not_shift_speaker_annotations(session: Session) -> None:
    """另起一句 inside sentence 0 must not move sentence 1's label onto it.

    Before the fix: rows were matched by ord, so the new row at ord 1 inherited
    the label of the sentence that used to sit there (speaker 1), and the split
    half of speaker 0's sentence was suddenly attributed to the other person.
    """
    version = seed_version(session, text="一二三四五六", sizes=[6])
    annotate(session, version.id, [0])

    # Split the single sentence in half. Ord 1 is a brand-new row.
    editing.apply_boundary(session, version.id, action="new_sentence", from_token=3, to_token=3)
    assert speakers_of(session, version.id) == [0, 0]


def test_split_inherits_the_speaker_of_the_sentence_it_came_from(session: Session) -> None:
    """The new right-hand sentence belongs to the same speaker as its source."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    editing.apply_boundary(session, version.id, action="new_sentence", from_token=1, to_token=1)
    # 0 split into 0+1, so: [src0, src0's tail, src1].
    assert speakers_of(session, version.id) == [0, 0, 1]


def test_merge_keeps_the_surviving_sentences_label(session: Session) -> None:
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [None, 1])

    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    # One annotation across the merge: it survives rather than being lost.
    assert speakers_of(session, version.id) == [1]


def test_split_then_merge_roundtrip_keeps_labels(session: Session) -> None:
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    # 另起一句 inside sentence 0, then 并入上句 on the whole new sentence: the
    # shape returns to two sentences and so must the annotations.
    editing.apply_boundary(session, version.id, action="new_sentence", from_token=1, to_token=1)
    assert speakers_of(session, version.id) == [0, 0, 1]

    editing.apply_boundary(session, version.id, action="to_prev", from_token=1, to_token=3)
    assert speakers_of(session, version.id) == [0, 1]


# --- the new restriction -------------------------------------------------


def test_merge_across_two_speakers_is_refused(session: Session) -> None:
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    with pytest.raises(editing.EditError) as caught:
        editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert caught.value.code == "speaker_conflict"
    assert caught.value.hint, "拒绝要给出下一步，不能只是说不行"
    # The refusal must not have written anything.
    assert speakers_of(session, version.id) == [0, 1]


def test_joining_a_whole_sentence_upward_is_refused_for_two_speakers(session: Session) -> None:
    """「并入上句」 on a whole sentence degrades to a merge, so it is refused too."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    with pytest.raises(editing.EditError) as caught:
        editing.apply_boundary(session, version.id, action="to_prev", from_token=3, to_token=6)
    assert caught.value.code == "speaker_conflict"


def test_moving_half_a_sentence_under_another_speaker_is_refused(session: Session) -> None:
    """Partial join also misaligns: the words change who they are attributed to."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    with pytest.raises(editing.EditError) as caught:
        editing.apply_boundary(session, version.id, action="to_prev", from_token=3, to_token=4)
    assert caught.value.code == "speaker_conflict"


def test_unannotated_neighbours_still_merge(session: Session) -> None:
    """The restriction only applies to two *different* labels."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [None, None])

    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert speakers_of(session, version.id) == [None]


def test_the_same_speaker_twice_still_merges(session: Session) -> None:
    """Two sentences by one person are one paragraph, and merging them is right."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 0])

    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)
    assert speakers_of(session, version.id) == [0]


def test_one_annotated_one_not_still_merges_and_keeps_the_label(session: Session) -> None:
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [None, 1])

    editing.apply_boundary(session, version.id, action="to_prev", from_token=3, to_token=6)
    assert speakers_of(session, version.id) == [1]


# --- history ------------------------------------------------------------


def test_undo_restores_speaker_annotations(session: Session) -> None:
    """Undo used to restore ranges only, leaving every label on a shifted ord."""
    version = seed_version(session, text="一二三四五六", sizes=[3, 3])
    annotate(session, version.id, [0, 1])

    # Move half of sentence 1 up: same speaker is not involved, so pick a
    # boundary edit that is allowed and changes the count.
    editing.apply_boundary(session, version.id, action="new_sentence", from_token=1, to_token=1)
    assert len(speakers_of(session, version.id)) == 3

    editing.undo_last(session, version.id)
    assert speakers_of(session, version.id) == [0, 1]


def test_old_history_without_speakers_still_undoes(session: Session) -> None:
    """A row written before this fix stores ``[start, end]`` with no speaker.

    It must still undo: an operation recorded yesterday cannot become
    un-undoable because the snapshot format changed today. Labels come back as
    NULL, which is what the old code effectively had.
    """
    import json

    from animatter.ids import new_id, utcnow_iso
    from animatter.models import EditOp

    version = seed_version(session, text="一二三四六", sizes=[2, 3])
    annotate(session, version.id, [0, 1])

    session.add(
        EditOp(
            id=new_id("op"),
            project_id=version.project_id,
            version_id=version.id,
            seq=1,
            op_type="boundary",
            payload_json=json.dumps({"action": "new_sentence", "before": [[0, 2], [2, 5]]}),
            undone=False,
            created_at=utcnow_iso(),
        )
    )
    session.flush()

    editing.undo_op(session, version.id)
    assert speakers_of(session, version.id) == [None, None]
    assert len(transcribe_service.load_sentences(session, version.id)) == 2
