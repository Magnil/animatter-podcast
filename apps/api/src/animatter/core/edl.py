"""Edit Decision List: turning a set of deletions into a rendering plan.

This is the module the whole editing pipeline stands on. Two properties are
non-negotiable (AGENTS.md §6.5):

1. **Deletions are the source of truth, kept segments are derived.** Users delete
   things; they do not specify what to keep. Deriving keeps from deletes means an
   edit is reversible by removing one deletion, without reconstructing anything.
2. **Every timestamp shown after rendering must be mapped through
   :class:`TimeMap`.** Subtitles, chapter marks and the transcript export all
   reference the original audio; without the mapping they drift further off with
   each deletion.

All functions here are pure: no database, no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass


class EdlError(ValueError):
    """A deletion set that cannot produce a valid render."""


#: Regions shorter than this are merged into a neighbouring deletion. Cutting a
#: sub-80ms island produces an audible click with no useful content (§6.5 step 4).
MIN_KEEP_MS = 80

#: Expansion used when a cut point is not already in silence. Prevents biting the
#: first or last phoneme off a word.
GUARD_MS = 20


@dataclass(frozen=True, slots=True)
class Deletion:
    """One request to remove ``[start_ms, end_ms)`` from the original audio."""

    start_ms: int
    end_ms: int
    source: str = "token"
    token_ids: tuple[int, ...] = ()

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class Keep:
    """A surviving span, expressed in both timelines."""

    orig_start: int
    orig_end: int
    new_start: int

    @property
    def duration(self) -> int:
        return self.orig_end - self.orig_start

    @property
    def new_end(self) -> int:
        return self.new_start + self.duration


@dataclass(frozen=True, slots=True)
class Clamp:
    """A deletion edge that was moved to avoid a hard cut mid-phoneme."""

    requested: int
    applied: int
    edge: str
    reason: str


@dataclass(frozen=True, slots=True)
class Edl:
    """Compiled plan: the keep list plus the mapping it implies."""

    original_duration_ms: int
    keeps: tuple[Keep, ...]
    deletions: tuple[Deletion, ...]
    clamps: tuple[Clamp, ...] = ()

    @property
    def output_duration_ms(self) -> int:
        return sum(keep.duration for keep in self.keeps)

    @property
    def removed_ms(self) -> int:
        return self.original_duration_ms - self.output_duration_ms

    @property
    def segment_count(self) -> int:
        return len(self.keeps)

    def time_map(self) -> TimeMap:
        return TimeMap(self.keeps, self.original_duration_ms)


@dataclass(frozen=True, slots=True)
class TimeMap:
    """Bidirectional mapping between original and rendered time.

    ``to_new`` is what subtitles and export use. ``to_old`` lets the editor jump
    from a rendered preview back to the corresponding point in the original.
    """

    keeps: tuple[Keep, ...]
    original_duration_ms: int

    def to_new(self, ms: int) -> int | None:
        """Map original → rendered. Returns ``None`` for a deleted instant.

        Keep ranges are half-open ``[orig_start, orig_end)``, so an instant equal
        to a keep's ``orig_end`` is *not* part of that keep. When the next keep
        starts there it is the next keep's first sample; when there is a deletion
        in between it is deleted and must map to ``None``.

        Getting this wrong is silent and corrosive: it maps the first deleted
        instant back onto the previous keep's tail, so a subtitle that should
        vanish reappears one frame early.
        """
        for keep in self.keeps:
            if keep.orig_start <= ms < keep.orig_end:
                return keep.new_start + (ms - keep.orig_start)
        # The instant may be the boundary between two adjacent keeps; it belongs
        # to the later one, which the loop above already handled.
        return None

    def to_old(self, ms: int) -> int | None:
        """Map rendered → original."""
        for keep in self.keeps:
            if keep.new_start <= ms < keep.new_end:
                return keep.orig_start + (ms - keep.new_start)
            if ms == keep.new_end:
                return keep.orig_end
        return None

    def to_new_clamped(self, ms: int) -> int:
        """Map original → rendered, snapping deleted instants to the nearest keep.

        Used for subtitle cues whose range may start inside a deletion; dropping
        them outright would lose text the user did not delete.
        """
        mapped = self.to_new(ms)
        if mapped is not None:
            return mapped
        before = [keep for keep in self.keeps if keep.orig_end <= ms]
        if before:
            return before[-1].new_end
        return 0

    def map_range(self, start_ms: int, end_ms: int) -> tuple[int, int] | None:
        """Map a span, returning ``None`` when nothing of it survives."""
        surviving = [keep for keep in self.keeps if keep.orig_start < end_ms and keep.orig_end > start_ms]
        if not surviving:
            return None
        first = surviving[0]
        last = surviving[-1]
        new_start = first.new_start + (max(start_ms, first.orig_start) - first.orig_start)
        new_end = last.new_start + (min(end_ms, last.orig_end) - last.orig_start)
        if new_end <= new_start:
            return None
        return (new_start, new_end)


@dataclass
class SilenceSpan:
    start_ms: int
    end_ms: int

    @property
    def midpoint(self) -> int:
        return (self.start_ms + self.end_ms) // 2

    @property
    def duration(self) -> int:
        return self.end_ms - self.start_ms


# --- normalisation ------------------------------------------------------


def normalize_deletions(deletions: list[Deletion], *, original_duration_ms: int) -> list[Deletion]:
    """Sort, clamp to the media, and merge overlapping or adjacent requests.

    Merging is what keeps the renderer's segment count sane: a user deleting 40
    characters inside one sentence should produce one cut, not forty.
    """
    cleaned: list[Deletion] = []
    for deletion in deletions:
        start = max(0, deletion.start_ms)
        end = min(original_duration_ms, deletion.end_ms)
        if end <= start:
            continue
        cleaned.append(
            Deletion(
                start_ms=start,
                end_ms=end,
                source=deletion.source,
                token_ids=deletion.token_ids,
            )
        )

    if not cleaned:
        return []

    cleaned.sort(key=lambda item: (item.start_ms, item.end_ms))
    merged: list[Deletion] = [cleaned[0]]
    for current in cleaned[1:]:
        previous = merged[-1]
        if current.start_ms <= previous.end_ms:
            merged[-1] = Deletion(
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, current.end_ms),
                source=previous.source,
                token_ids=previous.token_ids + current.token_ids,
            )
        else:
            merged.append(current)
    return merged


def find_silence_for(ms: int, silences: list[SilenceSpan], *, max_distance_ms: int) -> SilenceSpan | None:
    """Nearest silence whose midpoint is within ``max_distance_ms`` of ``ms``."""
    best: SilenceSpan | None = None
    best_distance = max_distance_ms + 1
    for silence in silences:
        distance = abs(silence.midpoint - ms)
        if distance < best_distance:
            best = silence
            best_distance = distance
    return best if best_distance <= max_distance_ms else None


def snap_to_silence(
    deletions: list[Deletion],
    silences: list[SilenceSpan],
    *,
    max_distance_ms: int = 150,
) -> tuple[list[Deletion], list[Clamp]]:
    """Expand each deletion edge into the nearby silence, up to ``max_distance_ms``.

    A cut inside a vowel is audible as a click or a clipped syllable, so sliding
    it into the adjacent quiet is the cheapest fix.

    **Snapping may only ever grow the deletion.** Picking the nearest silence edge
    without regard to direction can move an edge *inward* — for example a cut at
    6010 with silence at 5991–6100 would snap back to 5991, handing the user 19ms
    of audio they explicitly deleted and putting the very half-phoneme back that
    snapping exists to remove. So each edge snaps to the silence boundary on its
    own side of the cut only:

    * ``start`` may only move **left** (or stay), to ``silence.start_ms``.
    * ``end`` may only move **right** (or stay), to ``silence.end_ms``.
    """
    snapped: list[Deletion] = []
    clamps: list[Clamp] = []

    for deletion in deletions:
        start = deletion.start_ms
        end = deletion.end_ms

        start_silence = find_silence_for(start, silences, max_distance_ms=max_distance_ms)
        if start_silence is not None:
            # Only leftward: the audio just before the cut survives, so retreating
            # into silence removes more, never less.
            candidate = start_silence.start_ms
            if candidate < start:
                clamps.append(
                    Clamp(
                        requested=start,
                        applied=candidate,
                        edge="start",
                        reason="吸附到最近的静音段",
                    )
                )
                start = candidate

        end_silence = find_silence_for(end, silences, max_distance_ms=max_distance_ms)
        if end_silence is not None:
            # Only rightward, for the same reason mirrored.
            candidate = end_silence.end_ms
            if candidate > end:
                clamps.append(
                    Clamp(
                        requested=end,
                        applied=candidate,
                        edge="end",
                        reason="吸附到最近的静音段",
                    )
                )
                end = candidate

        if end > start:
            snapped.append(
                Deletion(
                    start_ms=start,
                    end_ms=end,
                    source=deletion.source,
                    token_ids=deletion.token_ids,
                )
            )
        else:
            clamps.append(
                Clamp(
                    requested=deletion.end_ms,
                    applied=deletion.start_ms,
                    edge="end",
                    reason="吸附后区间为空，已取消该删除",
                )
            )

    return snapped, clamps


def compile_edl(
    *,
    original_duration_ms: int,
    deletions: list[Deletion],
    silences: list[SilenceSpan] | None = None,
    snap_to_silences: bool = True,
    guard_ms: int = GUARD_MS,
    min_keep_ms: int = MIN_KEEP_MS,
) -> Edl:
    """Build the keep list from a deletion set.

    Steps mirror AGENTS.md §6.5: normalise → snap → guard → derive keeps →
    drop slivers → refuse an empty result.
    """
    if original_duration_ms <= 0:
        raise EdlError("音频时长无效")

    normalised = normalize_deletions(deletions, original_duration_ms=original_duration_ms)
    clamps: list[Clamp] = []

    if snap_to_silences and silences:
        normalised, clamps = snap_to_silence(normalised, silences)
        normalised = normalize_deletions(normalised, original_duration_ms=original_duration_ms)

    # Guard: nudge edges outward by a few ms so the first/last phoneme survives.
    guarded: list[Deletion] = []
    for deletion in normalised:
        start = max(0, deletion.start_ms - guard_ms)
        end = min(original_duration_ms, deletion.end_ms + guard_ms)
        guarded.append(
            Deletion(
                start_ms=start,
                end_ms=end,
                source=deletion.source,
                token_ids=deletion.token_ids,
            )
        )
    guarded = normalize_deletions(guarded, original_duration_ms=original_duration_ms)

    keeps_raw = _keeps_from_deletions(guarded, original_duration_ms)
    keeps = _drop_slivers(keeps_raw, min_keep_ms)

    if not keeps:
        raise EdlError("删除后没有剩余音频：请至少保留一段")

    # Re-number new_start after slivers were dropped, so the mapping stays dense.
    rebuilt: list[Keep] = []
    cursor = 0
    for keep in keeps:
        rebuilt.append(Keep(orig_start=keep.orig_start, orig_end=keep.orig_end, new_start=cursor))
        cursor += keep.duration

    return Edl(
        original_duration_ms=original_duration_ms,
        keeps=tuple(rebuilt),
        deletions=tuple(guarded),
        clamps=tuple(clamps),
    )


def _keeps_from_deletions(deletions: list[Deletion], duration_ms: int) -> list[Keep]:
    keeps: list[Keep] = []
    cursor = 0
    for deletion in deletions:
        if deletion.start_ms > cursor:
            keeps.append(Keep(orig_start=cursor, orig_end=deletion.start_ms, new_start=0))
        cursor = max(cursor, deletion.end_ms)
    if cursor < duration_ms:
        keeps.append(Keep(orig_start=cursor, orig_end=duration_ms, new_start=0))
    return keeps


def _drop_slivers(keeps: list[Keep], min_keep_ms: int) -> list[Keep]:
    """Remove islands too short to be useful; they render as clicks."""
    return [keep for keep in keeps if keep.duration >= min_keep_ms]


def edl_signature(edl: Edl) -> str:
    """Stable hash of a plan, used as the render cache key."""
    import hashlib

    payload = "|".join(f"{k.orig_start}-{k.orig_end}" for k in edl.keeps)
    payload += f"#{edl.original_duration_ms}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
