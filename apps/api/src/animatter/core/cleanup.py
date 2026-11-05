"""Planning a one-click cleanup: what would be cut, and where.

Three independent sources, deliberately kept separable so the user can tick one
and leave the others:

* **fillers** — the token text is on the 口癖 list (``core/fillers.py``);
* **disfluencies** — speech the ASR never transcribed. Whisper and Bailian both
  routinely drop a whole "嗯": the audio is still speech, the transcript has
  nothing there, so a text-only cleanup silently does half the job. Speech is
  the complement of the silence list, and uncovered speech is the complement of
  the token cover inside it;
* **silences** — gaps between kept words long enough to be dead air.

The hard rule, learned from Rescript's PR #115 ("Keep short words between
removed silences"): **two cuts may be merged, but never across a kept word.**
Our EDL drops keep-islands shorter than ``min_keep_ms``, so a 60 ms "对" between
two cut silences would vanish from the render while still showing in the
transcript — the worst kind of disagreement, because the user cannot see what
they lost.

Everything here is pure: tuples and :class:`TimeRange` in, plan out. No session,
no ffmpeg, no clock. The service layer does IO and persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from animatter.core.fillers import FillerMatch, is_filler, merge_adjacent_fillers

#: A gap must be at least this long to count as removable silence.
DEFAULT_MIN_SILENCE_MS = 300
#: Leave this much audio beside kept speech so onsets and offsets survive.
DEFAULT_SILENCE_PAD_MS = 50
#: Uncovered speech shorter than this is more likely ASR boundary slop than a
#: dropped filler; Rescript uses the same threshold for the same reason.
DEFAULT_MIN_DISFLUENCY_MS = 300
#: Absorb detector hangover at the start of an uncovered run that abuts a word,
#: so the proposed cut tracks the filler rather than the bleed after the word.
HANGOVER_TRIM_MS = 120
#: Bridge two silence cuts this close together — only when no kept word sits
#: between them. Matches Rescript's MERGE_GAP for the same rendering reason.
MERGE_GAP_MS = 350


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Half-open ``[start_ms, end_ms)``, matching the rest of the codebase."""

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class CleanupItem:
    """One proposed cut, with enough context for a human to judge it."""

    kind: str  # "filler" | "disfluency" | "silence"
    start_ms: int
    end_ms: int
    text: str = ""
    #: Token indices to delete, when the cut comes from words rather than audio.
    token_indices: tuple[int, ...] = ()
    #: Sentence ordinal, so the dialog can say "第 3 句".
    sentence_ord: int = -1

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    fillers: tuple[CleanupItem, ...] = ()
    disfluencies: tuple[CleanupItem, ...] = ()
    silences: tuple[CleanupItem, ...] = ()

    def items_for(self, kind: str) -> tuple[CleanupItem, ...]:
        if kind == "filler":
            return self.fillers
        if kind == "disfluency":
            return self.disfluencies
        if kind == "silence":
            return self.silences
        return ()

    def selected(self, kinds: set[str]) -> tuple[CleanupItem, ...]:
        """Items for the kinds the user ticked, in time order."""
        chosen: list[CleanupItem] = []
        for kind in ("filler", "disfluency", "silence"):
            if kind in kinds:
                chosen.extend(self.items_for(kind))
        return tuple(sorted(chosen, key=lambda item: (item.start_ms, item.end_ms)))

    def summary(self) -> dict[str, dict[str, int]]:
        """Per-kind counts. The UI shows these as the tick-box labels."""
        return {
            kind: {
                "items": len(self.items_for(kind)),
                "duration_ms": sum(item.duration_ms for item in self.items_for(kind)),
            }
            for kind in ("filler", "disfluency", "silence")
        }


# --- range arithmetic ---------------------------------------------------


def subtract(ranges: list[TimeRange], cuts: list[TimeRange]) -> list[TimeRange]:
    """Everything in ``ranges`` that no cut covers."""
    parts = list(ranges)
    for cut in cuts:
        if not parts:
            break
        nxt: list[TimeRange] = []
        for part in parts:
            if cut.end_ms <= part.start_ms or cut.start_ms >= part.end_ms:
                nxt.append(part)
                continue
            if cut.start_ms > part.start_ms:
                nxt.append(TimeRange(part.start_ms, cut.start_ms))
            if cut.end_ms < part.end_ms:
                nxt.append(TimeRange(cut.end_ms, part.end_ms))
        parts = nxt
    return parts


def complement(ranges: list[TimeRange], *, start_ms: int, end_ms: int) -> list[TimeRange]:
    """The gaps of ``[start_ms, end_ms)`` that none of ``ranges`` covers."""
    return subtract([TimeRange(start_ms, end_ms)], sorted(ranges, key=lambda r: r.start_ms))


# --- the three finders --------------------------------------------------


def find_filler_cuts(
    tokens: list[tuple[int, str, int, int]],
    *,
    fillers: frozenset[str] | set[str],
    sentence_of: dict[int, int] | None = None,
    deleted: set[int] | None = None,
) -> list[CleanupItem]:
    """Filler tokens, with adjacent ones already collapsed into one cut.

    The cut is exactly the tokens' span: it does **not** swallow the pause
    around it. Swallowing would make every 嗯 cut 0.4 s of real breathing room,
    and the user asked to remove the sound, not the silence — silence has its
    own switch with its own threshold.
    """
    skip = deleted or set()
    located = [
        FillerMatch(token_index=idx, text=text, start_ms=start_ms, end_ms=end_ms)
        for idx, text, start_ms, end_ms in tokens
        if idx not in skip and end_ms > start_ms and is_filler(text, fillers)
    ]
    if not located:
        return []

    return [
        CleanupItem(
            kind="filler",
            start_ms=run.start_ms,
            end_ms=run.end_ms,
            text=run.text,
            token_indices=run.token_indices,
            sentence_ord=(sentence_of or {}).get(run.token_index, -1),
        )
        for run in merge_adjacent_fillers(located)
    ]


def find_silence_cuts(
    tokens: list[tuple[int, str, int, int]],
    *,
    duration_ms: int,
    silences: list[TimeRange],
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
    pad_ms: int = DEFAULT_SILENCE_PAD_MS,
    deleted: set[int] | None = None,
) -> list[CleanupItem]:
    """Dead air to cut: detected silence, minus anything a kept word occupies.

    Using the detector's spans rather than raw inter-token gaps matters: ASR
    timestamps are tight, so a word-free gap often still contains audible breath
    the detector does not call silence. Cutting the gap would then clip it.
    """
    if duration_ms <= 0:
        return []

    skip = deleted or set()
    kept = sorted(
        (
            TimeRange(start_ms, end_ms)
            for idx, _t, start_ms, end_ms in tokens
            if idx not in skip and end_ms > start_ms
        ),
        key=lambda r: r.start_ms,
    )
    kept_starts = [r.start_ms for r in kept]
    kept_ends = [r.end_ms for r in kept]

    cuts: list[CleanupItem] = []
    for span in sorted(silences, key=lambda s: s.start_ms):
        start = max(span.start_ms, 0)
        end = min(span.end_ms, duration_ms)
        if end - start < min_silence_ms:
            continue
        for part in subtract([TimeRange(start, end)], kept):
            body = _pad_against_speech(part, kept_starts, kept_ends, pad_ms, duration_ms)
            if body is None or body.duration_ms <= 0:
                continue
            # After padding, a span may fall below the threshold; keep it only if
            # it is still a real pause. Half the threshold is the pragmatic line
            # (a 0.3s silence padded down to 0.2s is still dead air worth taking).
            if body.duration_ms * 2 < min_silence_ms:
                continue
            cuts.append(CleanupItem(kind="silence", start_ms=body.start_ms, end_ms=body.end_ms))

    return _merge_silence_cuts(cuts, kept)


def find_disfluencies(
    tokens: list[tuple[int, str, int, int]],
    *,
    duration_ms: int,
    silences: list[TimeRange],
    min_duration_ms: int = DEFAULT_MIN_DISFLUENCY_MS,
) -> list[CleanupItem]:
    """Speech with no transcript token covering it — dropped "嗯"s.

    Only not-deleted tokens count as cover. A word the user already cut leaves
    its audio uncovered, but proposing it again would double-count the same
    audio and confuse the "本次会删 N 处" number.
    """
    if duration_ms <= 0 or not silences:
        return []

    speech = complement(
        [TimeRange(max(0, s.start_ms), min(duration_ms, s.end_ms)) for s in silences],
        start_ms=0,
        end_ms=duration_ms,
    )
    covered = [TimeRange(start_ms, end_ms) for _i, _t, start_ms, end_ms in tokens if end_ms > start_ms]

    out: list[CleanupItem] = []
    for run in speech:
        for gap in subtract([run], covered):
            start, end = gap.start_ms, gap.end_ms
            # Hangover after a preceding word is not a filled pause. Only trim
            # when the run's start really does abut speech.
            if any(abs(edge - start) <= 2 for edge in (e for _i, _t, _s, e in tokens)):
                start = min(end, start + HANGOVER_TRIM_MS)
            if end - start < min_duration_ms:
                continue
            out.append(CleanupItem(kind="disfluency", start_ms=start, end_ms=end))
    return out


# --- helpers ------------------------------------------------------------


def _pad_against_speech(
    part: TimeRange,
    kept_starts: list[int],
    kept_ends: list[int],
    pad_ms: int,
    duration_ms: int,
) -> TimeRange | None:
    """Hold back ``pad_ms`` of audio wherever the cut touches real speech.

    Media edges are cut flush: a file must not begin or end with a stray
    fraction of a second, and there is nothing to protect outside the file.
    """
    start = part.start_ms
    end = part.end_ms
    touches_previous = any(abs(edge - start) <= 2 for edge in kept_ends)
    touches_next = any(abs(edge - end) <= 2 for edge in kept_starts)
    if touches_previous:
        start = min(end, start + pad_ms)
    if touches_next:
        end = max(start, end - pad_ms)
    if start <= 0 and end >= duration_ms:
        return None
    return TimeRange(max(0, start), min(duration_ms, end))


def _merge_silence_cuts(items: list[CleanupItem], kept: list[TimeRange]) -> list[CleanupItem]:
    """Bridge nearby silence cuts, but never across a kept word.

    This is the rule that stops "remove silences" from eating a short word
    sandwiched between two pauses — the exact regression Rescript had to fix
    after shipping, so it gets a test here rather than a bug report later.
    """
    if not items:
        return []

    ordered = sorted(items, key=lambda item: (item.start_ms, item.end_ms))
    out: list[CleanupItem] = [ordered[0]]
    for item in ordered[1:]:
        previous = out[-1]
        if item.start_ms - previous.end_ms >= MERGE_GAP_MS:
            out.append(item)
            continue
        if any(word.end_ms > previous.end_ms and word.start_ms < item.start_ms for word in kept):
            out.append(item)
            continue
        out[-1] = CleanupItem(
            kind="silence",
            start_ms=previous.start_ms,
            end_ms=max(previous.end_ms, item.end_ms),
        )
    return out


@dataclass
class PlanRequest:
    """Everything planning needs, gathered by the service layer."""

    tokens: list[tuple[int, str, int, int]] = field(default_factory=list)
    duration_ms: int = 0
    silences: list[TimeRange] = field(default_factory=list)
    fillers: frozenset[str] = frozenset()
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS
    silence_pad_ms: int = DEFAULT_SILENCE_PAD_MS
    min_disfluency_ms: int = DEFAULT_MIN_DISFLUENCY_MS
    sentence_of: dict[int, int] | None = None
    deleted: set[int] | None = None


def build_plan(request: PlanRequest) -> CleanupPlan:
    """Compute all three kinds at once; the caller decides what to apply."""
    return CleanupPlan(
        fillers=tuple(
            find_filler_cuts(
                request.tokens,
                fillers=request.fillers,
                sentence_of=request.sentence_of,
                deleted=request.deleted,
            )
        ),
        disfluencies=tuple(
            find_disfluencies(
                request.tokens,
                duration_ms=request.duration_ms,
                silences=request.silences,
                min_duration_ms=request.min_disfluency_ms,
            )
        ),
        silences=tuple(
            find_silence_cuts(
                request.tokens,
                duration_ms=request.duration_ms,
                silences=request.silences,
                min_silence_ms=request.min_silence_ms,
                pad_ms=request.silence_pad_ms,
                deleted=request.deleted,
            )
        ),
    )
