"""One cheap read of a version's transcript, shared by every consumer in a request.

**Why this module exists.** A still preview used to spend ~414 ms of its ~1020 ms in Python,
and almost none of it was arithmetic. Measured on the 8180-token 费那奇MV transcript:

===========================  ==============
operation                    median
===========================  ==============
8180 tokens as **ORM rows**  **92 ms**
8180 tokens as 3 columns     14.5 ms
8180 tokens as 6 columns     22.7 ms
583 sentences as columns     1.4 ms
===========================  ==============

and the avatar path loaded those ORM rows **once per bound slot** (4 on the sample), while
the subtitle path loaded them again. Five loads of the same immutable rows, ~400 ms, inside
one HTTP request whose answer cannot change while it runs.

Loading them once as columns and handing the result to both consumers removes that without
adding a cache: an LRU would need an invalidation key and would be wrong the moment an edit
landed between two requests. This has no invalidation surface at all — the data lives
exactly as long as the request that read it.

Why columns rather than ORM rows: the ORM path builds 8180 Python objects with identity-map
registration and cost **6.3×** more than projecting the three columns the user-avatar
arithmetic actually needs, and 4× more than the six the cue builder needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.models import Sentence, Token


@dataclass(frozen=True, slots=True)
class TokenRow:
    """The columns the picture pipeline needs from one token.

    Deliberately **not** ``TokenSpan``: that one carries eleven fields for the editor, and
    building 8180 of them costs what this module exists to avoid.
    """

    start_ms: int
    end_ms: int
    text: str
    punc_after: str
    hidden: bool


@dataclass(frozen=True, slots=True)
class SentenceRow:
    """The columns the picture pipeline needs from one sentence.

    ``token_end`` is **exclusive**; ``token_start``/``token_end`` index ``Token.idx``, not a
    list position, so a gap in the index cannot shift the slice onto the wrong words.
    """

    ord: int
    token_start: int
    token_end: int
    speaker_id: int | None


@dataclass(frozen=True, slots=True)
class TranscriptFacts:
    """A version's transcript, read once, in a form both consumers can use."""

    tokens: dict[int, TokenRow]
    sentences: tuple[SentenceRow, ...]

    def tokens_between(self, start: int, end: int) -> list[TokenRow]:
        """The tokens of one sentence, in order.

        ``Token.idx`` is dense (``(version_id, idx)`` is unique and every version written by
        the transcriber starts at 0), but the lookup is by id anyway: a positional slice
        would silently take the *neighbouring* words if the invariant ever broke, and every
        subtitle would then be off by one sentence with no error to notice.
        """
        return [
            self.tokens[index]
            for index in range(start, end)
            if index in self.tokens
        ]


def load(session: Session, version_id: str) -> TranscriptFacts:
    """Read the transcript as columns — the cheap form.

    Two queries, both of which come back in ~24 ms total on a 3-hour episode; the ORM form
    of the same data is ~92 ms.
    """
    token_rows = session.execute(
        select(
            Token.idx,
            Token.start_ms,
            Token.end_ms,
            Token.text,
            Token.punc_after,
            Token.hidden,
        ).where(Token.version_id == version_id)
    ).all()
    sentence_rows = session.execute(
        select(
            Sentence.ord,
            Sentence.token_start,
            Sentence.token_end,
            Sentence.speaker_id,
        )
        .where(Sentence.version_id == version_id)
        .order_by(Sentence.ord)
    ).all()

    return TranscriptFacts(
        tokens={
            row[0]: TokenRow(
                start_ms=row[1],
                end_ms=row[2],
                text=row[3],
                punc_after=row[4],
                hidden=row[5],
            )
            for row in token_rows
        },
        sentences=tuple(
            SentenceRow(
                ord=row[0],
                token_start=row[1],
                token_end=row[2],
                speaker_id=row[3],
            )
            for row in sentence_rows
        ),
    )
