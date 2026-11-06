"""Audacity label files: one tab-separated line per word.

This is the cheapest interchange format we have, and the only one that puts the
word-level timeline into a tool the user already edits in. Audacity turns the
file into a label track (File > Import > Labels); clicking a label selects that
region of the original audio, so "delete the filler" becomes one keystroke
instead of a round trip through our renderer.

The format is unforgiving in one specific way: a line without exactly three
tab-separated fields is **silently dropped** on import. Nothing warns, the track
just comes out short, which reads as "the export lost half the words". So every
field is sanitised here rather than trusted downstream.

Format (Audacity manual, "Importing and Exporting Labels"):

    <start seconds>\\t<end seconds>\\t<label text>

Start equal to end is a point label; unequal is a region. Seconds are decimal
with a period, never a comma. The file must be UTF-8 for non-ASCII labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from animatter.core.fillers import is_filler

#: Tag prefixes. Square brackets are the convention the reference script used
#: and the one that reads well in Audacity's narrow label column, where a longer
#: marker pushes the actual word out of view.
TAG_FILLER = "[FILLER]"
TAG_SILENCE = "[SILENCE]"

#: Characters that would break the three-field contract, plus the ones Windows
#: refuses in a filename (labels end up in a filename when exported per label).
_FORBIDDEN = str.maketrans({"\t": " ", "\r": " ", "\n": " ", "\x00": ""})


@dataclass(frozen=True, slots=True)
class LabelLine:
    """One label region, in integer milliseconds like everything else."""

    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def clean_text(text: str) -> str:
    """Make a string safe to be the third field of a label line."""
    return str(text).translate(_FORBIDDEN).strip()


def build_labels(
    tokens: list[tuple[int, str, int, int]],
    *,
    silences: Sequence[tuple[int, int]] = (),
    fillers: frozenset[str] | set[str] = frozenset(),
    include_filler: bool = True,
    include_silence: bool = True,
    deleted: set[int] | None = None,
    skip_deleted: bool = False,
) -> list[LabelLine]:
    """Word labels, plus the two tags worth seeing before you cut.

    ``tokens`` is ``(idx, text, start_ms, end_ms)`` — the same plain tuple the
    cleanup core takes, so this stays a pure function with no persistence
    dependency.

    Deliberately no stutter/dysfluency detection here. Those heuristics are
    worth a guess, never a certainty, and a wrong ``[MISTAKE]`` tag costs more
    than a missing one: the user trusts the tag, cuts the region, and loses a
    real word. Silence and the user's own filler list are facts we already hold.
    """
    skip = deleted or set()
    out: list[LabelLine] = []

    for idx, text, start_ms, end_ms in tokens:
        if skip_deleted and idx in skip:
            continue
        body = clean_text(text)
        if not body:
            # A token with no text is an alignment artefact, not a word. Writing
            # it would put an unlabelled region in the track that selects audio
            # with nothing to tell the user why.
            continue
        if include_filler and is_filler(text, fillers):
            body = TAG_FILLER + body
            # The filler list is a user-editable textarea, so a pasted entry can
            # carry spaces; the tag has to stay glued to the word it describes.
        out.append(LabelLine(start_ms=start_ms, end_ms=end_ms, text=body))

    if include_silence:
        for start_ms, end_ms in silences:
            if end_ms <= start_ms:
                continue
            out.append(LabelLine(start_ms=start_ms, end_ms=end_ms, text=TAG_SILENCE))

    # Stable on (start, end) keeps the file readable when a silence span and a
    # word share a boundary — which they routinely do, by construction.
    out.sort(key=lambda line: (line.start_ms, line.end_ms))
    return out


def format_seconds(ms: int) -> str:
    """Milliseconds to the three-decimal seconds Audacity expects.

    Fixed 3 decimals, never ``7.8``: Audacity parses either, but a column that
    is not the same width is hard to eyeball, and the reference file the user
    works with uses 3 everywhere.
    """
    return f"{ms / 1000:.3f}"


def render_audacity(lines: list[LabelLine]) -> bytes:
    """Serialise to the exact byte shape Audacity and the user both expect.

    CRLF and a trailing newline, UTF-8 without BOM. The reference file uses
    CRLF because it was produced on Windows; Audacity accepts LF too, but a
    file that opens identically in Notepad and in Audacity is one less thing to
    explain when someone reports "乱码".
    """
    body = "".join(
        f"{format_seconds(line.start_ms)}\t{format_seconds(line.end_ms)}\t{line.text}\r\n" for line in lines
    )
    return body.encode("utf-8")


def suggest_filename(project_name: str, *, version: int = 1) -> str:
    """``漫物质_audacity_labels.txt``, safe on Windows and readable in the dialog.

    The stem is the project name because that is what the user recognises; the
    old reference script used the audio file's stem, which for us is always the
    useless ``original``.
    """
    bad = '<>:"/\\|?*'
    stem = "".join("_" if ch in bad or ord(ch) < 32 else ch for ch in project_name).strip(" .")
    if not stem:
        stem = "labels"
    suffix = "" if version <= 1 else f"_v{version}"
    return f"{stem}{suffix}_audacity_labels.txt"
