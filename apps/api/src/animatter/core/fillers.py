"""Filler-word (口癖) detection: a list, a normaliser, and a matcher.

Deliberately conservative. The most common mistake a filler remover can make is
deleting a word that was doing real work — "就是说" is a discourse marker in one
sentence and the actual predicate in another, and once the audio is gone the user
has to find the cut and restore it. Missing a filler costs a manual delete;
deleting a real word costs trust.

So the default list holds only sounds that carry no lexical meaning on their own
(嗯/呃/唔/um/uh/äh/…). Discourse markers are *not* included; they can be added per
user through 设置 → 一键清理, because a host's verbal tic is personal and any
fixed list is wrong for someone.

Matching is on the **token text**, stripped of punctuation, case-folded. It is
never a substring search: at word level one token is one ASR word, so "嗯" inside
"嗯嗯" is a different token and must be judged on its own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Default list. Grouped by language so adding a language is one block, not a
#: scatter of one-off entries.
DEFAULT_FILLERS: tuple[str, ...] = (
    # -- Chinese ---------------------------------------------------------
    # Only sounds. 啊/哦 are excluded on purpose: they are also real
    # interjections ("啊，原来如此") and the boundary is not detectable from text.
    "嗯",
    "嗯嗯",
    "嗯嗯嗯",
    "呃",
    "呃呃",
    "额",
    "额额",
    "唔",
    "唔唔",
    "呣",
    "欸",
    # -- English ---------------------------------------------------------
    "um",
    "umm",
    "ummm",
    "uh",
    "uhh",
    "uhm",
    "erm",
    "er",
    "err",
    "ah",
    "ahh",
    "eh",
    "ehm",
    "hm",
    "hmm",
    "hmmm",
    "mh",
    "mhh",
    "mm",
    "mmm",
    "mhm",
    "mm-hmm",
    "uh-huh",
    # -- German ----------------------------------------------------------
    "äh",
    "ähh",
    "ähhh",
    "ähm",
    "ähmm",
    "öh",
    "öhm",
    "ööhm",
    # -- French ----------------------------------------------------------
    "euh",
    "euuh",
    "euuuh",
    "euhm",
    "heu",
    "heum",
    # -- Spanish / Portuguese --------------------------------------------
    "em",
    "emm",
    "emmm",
    "eee",
    "hã",
    "hãhã",
    "ahn",
    "ahnn",
    "ãh",
    "ãhh",
    "ãhm",
    "éh",
    "éhh",
)

#: Terms a fixed list must never contain, enforced by a test rather than by
#: discipline: these are ordinary words in the languages we support.
FORBIDDEN_DEFAULTS: frozenset[str] = frozenset(
    {
        "啊",
        "哦",
        "噢",
        "呀",
        "哎",
        "唉",
        "这个",
        "那个",
        "就是",
        "就是说",
        "然后",
        "然后呢",
        "所以",
        "其实",
        "你知道",
        "怎么说",
        "like",
        "so",
        "right",
        "you know",
        "well",
        "actually",
        "basically",
    }
)

#: Leading/trailing characters stripped before matching: punctuation, quotes,
#: brackets, and the ellipsis the placeholder uses.
_EDGE_JUNK = re.compile(
    r"^[\s\u3000，。！？；：、,.!?;:\"'“”‘’（）()《》〈〉「」『』【】\[\]…—\-\u2014\u2013]+"
    r"|[\s\u3000，。！？；：、,.!?;:\"'“”‘’（）()《》〈〉「」『』【】\[\]…—\-\u2014\u2013]+$"
)


def normalize(text: str) -> str:
    """Fold a token to its comparable form.

    NFKC first so full-width Latin (＂ｕｍ＂) and half-width match, then case
    fold, then strip edge punctuation. Interior punctuation is kept: "mm-hmm"
    and "uh-huh" are single fillers and must not be split into two.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _EDGE_JUNK.sub("", folded)


def parse_word_list(raw: str) -> list[str]:
    """Turn the settings textarea into a clean list.

    One term per line; commas and the Chinese enumeration comma also separate,
    because people paste lists from documents. Deduplicated case-insensitively,
    order preserved (the user's ordering is the only signal we have).
    """
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[\n\r,，、;；]+", raw):
        term = normalize(chunk)
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out


def format_word_list(terms: list[str] | tuple[str, ...]) -> str:
    """Inverse of :func:`parse_word_list`, for the settings textarea."""
    return "\n".join(terms)


@dataclass(frozen=True, slots=True)
class FillerMatch:
    """One token judged to be a filler."""

    token_index: int
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class FillerRun:
    """Consecutive filler tokens collapsed into one cut."""

    token_indices: tuple[int, ...]
    text: str
    start_ms: int
    end_ms: int

    @property
    def token_index(self) -> int:
        return self.token_indices[0] if self.token_indices else -1

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def is_filler(text: str, fillers: frozenset[str] | set[str]) -> bool:
    """Whether one token's text is on the list."""
    term = normalize(text)
    return bool(term) and term in fillers


def find_fillers(
    tokens: list[tuple[int, str, int, int]],
    fillers: frozenset[str] | set[str],
    *,
    deleted: set[int] | None = None,
) -> list[FillerMatch]:
    """Locate every not-yet-deleted filler token.

    ``tokens`` is ``(idx, text, start_ms, end_ms)`` — a plain tuple rather than
    the :class:`TokenSpan` dataclass so this stays a pure core function with no
    dependency on the persistence layer.
    """
    skip = deleted or set()
    out: list[FillerMatch] = []
    for idx, text, start_ms, end_ms in tokens:
        if idx in skip:
            continue
        if is_filler(text, fillers):
            out.append(FillerMatch(token_index=idx, text=text, start_ms=start_ms, end_ms=end_ms))
    return out


def merge_adjacent_fillers(matches: list[FillerMatch]) -> list[FillerRun]:
    """Collapse runs like 嗯 嗯 嗯 into one cut spanning all three.

    The count the UI shows should be "40 places to cut", not "97 tokens": the
    user thinks in cuts, and three consecutive 嗯 is one cut. The texts are
    joined so the list can say what is going.
    """
    if not matches:
        return []

    ordered = sorted(matches, key=lambda m: m.token_index)
    out: list[FillerRun] = []
    indices = [ordered[0].token_index]
    texts = [ordered[0].text]
    start = ordered[0].start_ms
    end = ordered[0].end_ms
    last_index = ordered[0].token_index

    for match in ordered[1:]:
        if match.token_index == last_index + 1:
            indices.append(match.token_index)
            texts.append(match.text)
            end = match.end_ms
            last_index = match.token_index
            continue
        out.append(FillerRun(tuple(indices), "".join(texts), start, end))
        indices = [match.token_index]
        texts = [match.text]
        start = match.start_ms
        end = match.end_ms
        last_index = match.token_index

    out.append(FillerRun(tuple(indices), "".join(texts), start, end))
    return out
