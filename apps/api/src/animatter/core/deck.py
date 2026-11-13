"""Deck planning: turning a transcript into topic pages, with no IO (§19.6, §19.15).

What "one page" means
---------------------
A page is a **topic**, not a stretch of time. The boundary that matters is where the
conversation moves on to something else, and that is a semantic judgement no threshold can
make: measured on the sample episode, sentence gaps are p50 320 ms, so "break at a pause"
degenerates into "break at the length cap", and a length cap breaks mid-topic every minute
whether or not anything changed. 583 sentences became 60 pages of verbatim transcript — a
teleprompter, not a deck.

So the model plans the pages (:func:`parse_outline` turns its reply into pages) and
:func:`plan_pages` is kept as the **fallback**: deterministic, free, covers the whole
episode, and every page it produces is honest about being the transcript.

The model's job is deliberately small, and it mirrors :mod:`animatter.core.script`'s chapter
rule: it is asked which **line** a page starts on and what the page says, never a timestamp.
Timestamps are read back off the transcript, because a model writing ``00:12:34`` produces a
plausible number that is wrong often enough to be useless.

Everything after the model speaks is deterministic repair: the deck must start at the first
sentence, pages must be in order, a page must last long enough to read, and the deck must not
grow past a printable count. Each rule is applied here, in pure functions, and each one has a
test that shows what it refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

from animatter.core.script import Chapter, ScriptSentence, Turn

#: Longest a page may stay on screen under the **fallback** rule before it is cut regardless
#: of what follows. Measured on the real 61.5-minute episode (17,294 characters, 4.69
#: chars/s): 60 s lands at ~293 characters per page, which sets at 44–48 px in the sample box.
TARGET_PAGE_MS: Final[int] = 60_000

#: How long a fallback page must already be before a mere pause is worth breaking at. With
#: this pair the observed page lengths are p10 54 s / p50 63 s / p90 71 s.
SEAM_AFTER_MS: Final[int] = 45_000

#: What counts as a pause worth breaking at, measured on the real episode: sentence gaps are
#: p50 320 ms, p75 520 ms, p90 830 ms. So ~700 ms is roughly "a real beat".
PAGE_GAP_MS: Final[int] = 700

#: How much text one fallback page carries before it is cut regardless of time. 480 CJK
#: characters sets at 32 px / 13 lines in the sample box, which is the smallest size that
#: still reads at 1080p.
MAX_PAGE_CHARS: Final[int] = 480

#: A fallback page shorter than this flashes; it is merged forward instead.
MIN_PAGE_MS: Final[int] = 8_000

#: A **topic** page shorter than this is not a topic, it is a flicker — the model occasionally
#: emits a one-sentence page where it changed its mind. Merged into its neighbour.
MIN_TOPIC_MS: Final[int] = 20_000

#: Hard cap on the page count. A model asked for "one page per topic" on a three-hour episode
#: can return 200 of them; past this the deck stops being a deck. Over the cap, the shortest
#: adjacent pair is merged until it fits — merging keeps the coverage, truncating would not.
MAX_PAGES: Final[int] = 60

#: How many sentences of the source text to keep as the page's context in the editor. The
#: body is trimmed to MAX_PAGE_CHARS; the full stretch is kept for the proofreading pane.
CONTEXT_CHARS: Final[int] = 900

#: Title length cap, same convention as the ShowNotes titles (``MAX_TITLE_CHARS``).
MAX_TITLE_CHARS: Final[int] = 26

#: Bullet marker. A full-width 「・」 rather than markdown's ``-``: the slide renderer draws
#: body lines verbatim, and an ASCII hyphen at 40 px looks like a rendering accident. One
#: character also keeps the width arithmetic honest in :mod:`animatter.core.slide`.
POINT_MARKER: Final[str] = "・"

#: Points per page. Four is what fits under a title at 1440×810 without shrinking the body
#: below 32 px; a fifth only makes the slide look like a paragraph again.
MAX_POINTS: Final[int] = 4

#: One point's length. Beyond this it is a sentence, and the slide stops being scannable.
MAX_POINT_CHARS: Final[int] = 28

#: How close a page start must be to a chapter start to be *that* chapter, in ms. A chapter
#: written by hand sits up to 6 s off the sentence it names; 5 s keeps the retitle local
#: without letting a nearby page steal the heading.
CHAPTER_SNAP_MS: Final[int] = 5_000

#: Where a fallback title may be cut. Ordered by nothing — :func:`_title_from` takes the
#: earliest position of any of them, because the first *clause* is what a reader scans.
_CLAUSE_MARKS: Final[tuple[str, ...]] = ("。", "！", "？", "；", "，")

#: A leading bullet or a leading number, which some models add even when told not to.
_POINT_PREFIX: Final[re.Pattern[str]] = re.compile(r"^(?:[-・•*·—–]+\s*|\d+[.、)）]\s*)+")

#: The system prompt for the outline call (round 100). Kept here, next to the parser that
#: enforces it, for the same reason the ShowNotes prompt lives in the parser's module: the two
#: drift apart the moment they are in different files.
DEFAULT_OUTLINE_PROMPT: Final[str] = """\
你是播客的演示稿编辑。给你一期播客的转写稿，每行一个发言段落，行首是行号。

把这期节目整理成一份演示稿：按话题转折分成若干页，每页写清这一段讲了哪几个小话题。

分段规则（最重要）：
1. 只在话题真正转折处分段：换了一个要讨论的问题、一件事讲完了、主持人起了新话头、
   从举例回到结论。同一件事的来龙去脉、举例、追问、插科打诨都留在同一页。
2. 不要按说话人分段。一问一答是同一段话题，不是两段。
3. 不要按时长分段。一页通常 2–4 分钟口播，但话题长短是它自己的事。
4. 第一页的行号必须是 0；后面每页的行号取该话题开始的那一行的行号，必须递增。
   一期 60 分钟的节目通常 15–40 页，不要超过 40 页。

每页的规则：
5. title：这一页的名字，不超过 16 字，是「叫什么」不是「说了什么」。
   不要写成句子，不要带书名号以外的标点。
6. points：2–4 条小话题或论点，每条不超过 20 字，写「他说了什么」，
   不要写「讨论了……」「提到了……」。可以是并列的论点、也可以是一件事的几个层面。
   points 是一个**纯字符串数组**：不要嵌套数组，不要包成对象，不要漏掉这一页其他几条。
7. 不要编造转写稿里没有的数字、人名、作品名、年份。

输出 JSON，不要任何解释：
{"pages": [{"line": 0, "title": "开场：为什么做这期", "points": ["第一条", "第二条"]}]}
"""


@dataclass(frozen=True)
class PagePlan:
    """One page before it becomes a row: where it starts and what it says."""

    #: Sentence ord of the first sentence on this page — what the editor jumps to.
    source_ord: int
    #: Absolute start on the **original** timeline. The video block maps it through the EDL.
    start_ms: int
    #: Last sentence ord covered, inclusive. Used to build the proofreading context.
    last_ord: int
    title: str
    #: The page's sub-topics, one per line, without the bullet marker. Empty on the fallback
    #: path and on a chapter page, which has a title and nothing else.
    points: tuple[str, ...] = ()
    #: Verbatim text, set only by the fallback planner. Never both with ``points``.
    text: str = ""
    #: The whole stretch's text, for the editor's left pane. Never rendered.
    context: str = ""
    speaker: int | None = None
    #: ``title`` for a page with no body (a chapter break); otherwise ``title_body``.
    layout: str = "title_body"

    @property
    def body(self) -> str:
        """What the renderer draws under the title.

        Derived rather than stored, so the marker cannot drift from the points: a page whose
        body was edited into something else is a row, not a plan.
        """
        if self.points:
            return "\n".join(f"{POINT_MARKER}{point}" for point in self.points)
        return self.text[:MAX_PAGE_CHARS]


def _clean(text: str) -> str:
    """Collapse whitespace so a title or a line of transcript is one flowing run.

    Newlines in a transcript are an artefact of sentence segmentation, not meaning.
    """
    return " ".join(text.split())


def _title_from(text: str) -> str:
    """A provisional fallback title: the stretch's first clause, cut to the cap.

    The **earliest** clause mark wins, not the first mark *type*: taking 「。」 first would
    return a whole sentence on every page, which is a title nobody can scan.
    """
    body = _clean(text)
    cut = min(
        (index for mark in _CLAUSE_MARKS if 4 <= (index := body.find(mark)) <= MAX_TITLE_CHARS),
        default=-1,
    )
    if cut >= 0:
        return body[:cut]
    return body[:MAX_TITLE_CHARS] if len(body) > MAX_TITLE_CHARS else body


# --- the model's outline -------------------------------------------------


def clean_points(raw: object) -> tuple[str, ...]:
    """Normalise whatever the model called "points" into at most four short lines.

    Forgiving about shape and strict about content: a leading bullet or ``1.`` is stripped, a
    point longer than :data:`MAX_POINT_CHARS` is cut, duplicates are dropped, and the list is
    capped. A slide with a 60-character bullet is a paragraph with a dot in front of it, and
    the renderer would answer by shrinking every line to 22 px.

    **Nested lists are flattened, not skipped.** Measured on a real reply: the model drifted
    mid-answer from ``["甲", "乙"]`` to ``["甲", ["乙"], ["丙"]]`` — still four points per page
    by its own count, but only one survived this function, so 16 of 23 pages came back with a
    single bullet. Recursing costs three lines and removes the whole failure mode; skipping
    is what turned a formatting quirk into a deck nobody could read.
    """
    items: list[str] = []
    stack: list[object] = [raw]
    while stack:
        entry = stack.pop(0)
        if isinstance(entry, str):
            items.extend(re.split(r"[\n;；]+", entry))
        elif isinstance(entry, list | tuple):
            stack[0:0] = list(entry)
        elif isinstance(entry, dict):
            value = entry.get("text") or entry.get("point") or entry.get("title")
            if isinstance(value, str):
                items.append(value)
            elif isinstance(value, list | tuple):
                stack[0:0] = list(value)

    out: list[str] = []
    for item in items:
        text = _clean(_POINT_PREFIX.sub("", _clean(item)))
        if not text:
            continue
        if len(text) > MAX_POINT_CHARS:
            text = text[:MAX_POINT_CHARS].rstrip("，、,；;：: ")
        if text and text not in out:
            out.append(text)
    return tuple(out[:MAX_POINTS])


def parse_outline(
    payload: object, turns: list[Turn]
) -> tuple[list[PagePlan], list[str]]:
    """Turn the model's reply into pages, plus whatever a human should be told.

    Strict about the one thing that must be exact — the line number, because that is what
    becomes ``start_ms`` — and forgiving about everything else. A page whose line does not
    exist is **dropped**, not snapped to the nearest one: a page attached to the wrong
    sentence shows the wrong slide for the wrong topic, and nothing on screen says so.

    A page with points but no title takes its first point as the title; a page with a title
    but no points becomes a section page (``layout="title"``), which is a legitimate slide.
    A page with neither is dropped — it would be a blank slide held for minutes.
    """
    data = payload if isinstance(payload, dict) else {}
    raw = data.get("pages") or data.get("segments") or data.get("slides")
    items = raw if isinstance(raw, list) else []

    pages: list[PagePlan] = []
    warnings: list[str] = []
    bad_lines = 0
    dropped = 0
    long_titles = 0
    thin = 0

    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        raw_line = item.get("line", item.get("start_line", item.get("index")))
        try:
            line = int(raw_line)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            bad_lines += 1
            continue
        if not 0 <= line < len(turns):
            bad_lines += 1
            continue

        turn = turns[line]
        points = clean_points(item.get("points") or item.get("bullets") or item.get("subtopics"))
        title = _clean(str(item.get("title") or ""))
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS]
            long_titles += 1
        if not title and points:
            title = points[0]
        if not title and not points:
            dropped += 1
            continue
        if len(points) < 2:
            # A page with a single bullet is a heading with one sentence under it — it reads
            # as a bug, and it is one: the prompt asks for 2–4. Counted rather than padded
            # (inventing a second point is worse than showing one) and reported, because the
            # fix is a different model, not a different renderer.
            thin += 1

        pages.append(
            PagePlan(
                source_ord=turn.first_ord,
                start_ms=turn.start_ms,
                last_ord=turn.first_ord,
                title=title,
                points=points,
                context="",
                speaker=turn.speaker,
                layout="title_body" if points else "title",
            )
        )

    if bad_lines:
        warnings.append(f"{bad_lines} 页的行号在转写稿里不存在，已丢弃")
    if dropped:
        warnings.append(f"{dropped} 页既没有标题也没有要点，已丢弃")
    if long_titles:
        warnings.append(f"{long_titles} 个页标题超过 {MAX_TITLE_CHARS} 字，已截断")
    if thin:
        warnings.append(f"{thin} 页只写出 1 条要点，换个大一点的模型（如 qwen-max）重生成会更好")
    return pages, warnings


# --- deterministic repair ------------------------------------------------


def settle_pages(
    pages: list[PagePlan],
    *,
    sentences: list[ScriptSentence],
    total_ms: int,
    chapters: list[Chapter] | None = None,
    max_pages: int = MAX_PAGES,
    min_page_ms: int = MIN_TOPIC_MS,
) -> tuple[list[PagePlan], list[str]]:
    """Make a set of planned pages safe to render, in five steps.

    1. **Order and start.** Sorted by time, non-increasing page starts dropped (a page with
       no duration), and the first page pulled back to the episode's first sentence — the
       deck has to cover the opening, or the box shows nothing until the first page starts.
    2. **No flicker.** A page shorter than ``min_page_ms`` is merged into its neighbour,
       points and all.
    3. **No wall.** Past ``max_pages`` the shortest adjacent pair is merged repeatedly. This
       keeps every minute of the episode on some page; truncating the tail would silently
       drop the end of the conversation.
    4. **Chapters.** A planned page within :data:`CHAPTER_SNAP_MS` of a chapter is retitled
       with the chapter heading; a chapter with no page near it gets its own section page.
    5. **Context.** Each page's ``last_ord`` and proofreading context are read back off the
       sentence slice, which only becomes possible once the *next* page's start is known.
    """
    warnings: list[str] = []
    if not sentences:
        return [], warnings

    ordered = sorted(pages, key=lambda page: page.start_ms)

    kept: list[PagePlan] = []
    collapsed = 0
    for page in ordered:
        if kept and page.start_ms <= kept[-1].start_ms:
            collapsed += 1
            continue
        kept.append(page)
    if collapsed:
        warnings.append(f"{collapsed} 页与上一页时间点重合或更早，已丢弃")

    first = sentences[0]
    if kept and (kept[0].source_ord != first.ord or kept[0].start_ms != first.start_ms):
        if kept[0].start_ms - first.start_ms > CHAPTER_SNAP_MS:
            warnings.append(
                f"第一页从 {_clock(first.start_ms)} 起，之前的 "
                f"{(kept[0].start_ms - first.start_ms) // 1000} 秒并入了第一页"
            )
        kept[0] = replace(kept[0], source_ord=first.ord, start_ms=first.start_ms)

    merged = _merge_short(kept, total_ms=total_ms, min_page_ms=min_page_ms)
    if len(merged) < len(kept):
        warnings.append(f"{len(kept) - len(merged)} 页太短（不足 {min_page_ms // 1000} 秒），已并入相邻页")

    capped = _merge_to_cap(merged, total_ms=total_ms, max_pages=max_pages)
    if len(capped) < len(merged):
        warnings.append(f"页数超过 {max_pages}，合并最短的相邻页到 {len(capped)} 页")

    if chapters:
        capped = _apply_chapters(capped, chapters)

    finished: list[PagePlan] = []
    for index, page in enumerate(capped):
        last_ord = (
            capped[index + 1].source_ord - 1 if index + 1 < len(capped) else sentences[-1].ord
        )
        finished.append(
            replace(
                page,
                last_ord=max(page.source_ord, last_ord),
                context=_context_for(sentences, page.source_ord, last_ord),
            )
        )
    return finished, warnings


def _merge_short(
    pages: list[PagePlan], *, total_ms: int, min_page_ms: int
) -> list[PagePlan]:
    """Fold a too-short page into a neighbour. Never drops content.

    Forward first, which keeps the merged page's start where the short one began, so a page
    at the very beginning of the episode is absorbed by the page that follows it. The last
    page has nowhere to go forward to, so it goes backwards. One page alone stays short:
    there is nothing to merge it with, and a one-page deck for a 30-second clip is correct.

    Written as a loop over a copy rather than a single pass, because merging changes the
    *durations* of both neighbours — the page after a forward merge can now be long enough,
    and a single pass would have already made its decision from the old numbers.
    """
    out = list(pages)
    while len(out) > 1:
        short = next(
            (
                index
                for index in range(len(out))
                if _span_of(out, index, total_ms=total_ms) < min_page_ms
            ),
            None,
        )
        if short is None:
            break
        if short + 1 < len(out):
            out[short + 1] = _merge(out[short], out[short + 1])
            del out[short]
        else:
            out[short - 1] = _merge(out[short - 1], out[short])
            del out[short]
    return out


def _merge_to_cap(pages: list[PagePlan], *, total_ms: int, max_pages: int) -> list[PagePlan]:
    """Merge the shortest adjacent pair until the deck fits ``max_pages``."""
    out = list(pages)
    while len(out) > max_pages and len(out) > 1:
        spans = [_span_of(out, index, total_ms=total_ms) for index in range(len(out) - 1)]
        smallest = min(range(len(spans)), key=lambda index: (spans[index], index))
        out[smallest] = _merge(out[smallest], out[smallest + 1])
        del out[smallest + 1]
    return out


def _span_of(pages: list[PagePlan], index: int, *, total_ms: int) -> int:
    end = pages[index + 1].start_ms if index + 1 < len(pages) else total_ms
    return max(0, end - pages[index].start_ms)


def _merge(first: PagePlan, second: PagePlan) -> PagePlan:
    """One page carrying both pages' points, starting where the first one did."""
    points = clean_points([*first.points, *second.points])
    title = first.title or second.title
    text = " ".join(part for part in (first.text, second.text) if part).strip()
    return replace(
        first,
        title=title,
        points=points,
        text=text,
        layout="title_body" if (points or text) else first.layout,
        speaker=first.speaker if first.speaker is not None else second.speaker,
    )


def _apply_chapters(pages: list[PagePlan], chapters: list[Chapter]) -> list[PagePlan]:
    """Chapter headings win over whatever the planner called that page.

    A chapter is a human's own naming of a stretch, so it replaces the planned title when a
    page starts close enough to it. A chapter with no page near it becomes a section page:
    the heading is the name of the stretch **starting** there, and dropping it would lose
    the one piece of structure a human wrote by hand.
    """
    out = list(pages)
    for chapter in sorted(chapters, key=lambda item: item.start_ms):
        heading = _clean(chapter.heading)[:MAX_TITLE_CHARS]
        if not heading:
            continue
        nearest = min(
            range(len(out)),
            key=lambda index: abs(out[index].start_ms - chapter.start_ms),
            default=-1,
        )
        if nearest >= 0 and abs(out[nearest].start_ms - chapter.start_ms) <= CHAPTER_SNAP_MS:
            out[nearest] = replace(out[nearest], title=heading)
            continue
        inserted = PagePlan(
            source_ord=chapter.first_ord,
            start_ms=chapter.start_ms,
            last_ord=chapter.first_ord,
            title=heading,
            layout="title",
        )
        out.append(inserted)
        out.sort(key=lambda page: page.start_ms)
    return out


def _context_for(sentences: list[ScriptSentence], first_ord: int, last_ord: int) -> str:
    parts = [item.text for item in sentences if first_ord <= item.ord <= last_ord]
    return _clean("".join(parts))[:CONTEXT_CHARS]


def _clock(ms: int) -> str:
    """``12:34`` — two groups, for a hint inside a warning rather than a published chapter."""
    total = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


# --- the fallback planner ------------------------------------------------


def plan_pages(
    sentences: list[ScriptSentence],
    *,
    max_page_chars: int = MAX_PAGE_CHARS,
    gap_ms: int = PAGE_GAP_MS,
    min_page_ms: int = MIN_PAGE_MS,
    target_page_ms: int = TARGET_PAGE_MS,
    seam_after_ms: int = SEAM_AFTER_MS,
) -> list[PagePlan]:
    """Group sentences into pages by pause and length. The fallback, not the default.

    Used when the model cannot plan: no API key, a failed call, or a reply that produced no
    usable page. It is deterministic, free and instant, and its pages are the transcript
    cut at pauses — which is exactly the thing the model path exists to replace, and exactly
    what a fallback should be: honest, complete, plainly inferior.

    Grouping runs over **sentences**, not turns: turns merge consecutive same-speaker
    sentences and would hide the pause between two of them, which is the boundary this can
    see. Two thresholds, not one: a page ends when it has run ``target_page_ms``, *or* when
    it has run ``seam_after_ms`` and a pause follows. A plain "break at any pause" rule
    produced 238 pages on a 61-minute episode.

    Returns an empty list for an empty transcript — "no pages" is a real state (an
    unprocessed project) that the caller reports, not an error.
    """
    usable = [item for item in sentences if item.text.strip()]
    if not usable:
        return []

    groups: list[list[ScriptSentence]] = []
    current: list[ScriptSentence] = []
    for item in usable:
        if current and _should_break(
            current,
            item,
            max_page_chars=max_page_chars,
            gap_ms=gap_ms,
            target_page_ms=target_page_ms,
            seam_after_ms=seam_after_ms,
        ):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)

    # A page too short to read is merged into the previous one. Doing this after grouping
    # (rather than preventing the split) keeps `_should_break` a simple, testable predicate.
    merged: list[list[ScriptSentence]] = []
    for group in groups:
        if merged and _span_ms(group) < min_page_ms:
            merged[-1].extend(group)
        else:
            merged.append(group)

    pages: list[PagePlan] = []
    for group in merged:
        text = _clean("".join(item.text for item in group))
        pages.append(
            PagePlan(
                source_ord=group[0].ord,
                start_ms=group[0].start_ms,
                last_ord=group[-1].ord,
                title=_title_from(text),
                text=text[:max_page_chars],
                context=text[:CONTEXT_CHARS],
                speaker=group[0].speaker,
            )
        )
    return pages


def _should_break(
    current: list[ScriptSentence],
    item: ScriptSentence,
    *,
    max_page_chars: int,
    gap_ms: int,
    target_page_ms: int,
    seam_after_ms: int,
) -> bool:
    """Whether ``item`` opens a new page.

    Three reasons, in order of how often they fire on real speech:

    1. the page already holds ``max_page_chars`` — never let one page become a wall;
    2. the page has run past ``target_page_ms`` — the length cap that keeps pages moving;
    3. the page has run past ``seam_after_ms`` **and** a pause follows — break at the natural
       seam rather than at the cap, which is why this check comes last and only applies once
       the page is old enough to be worth ending early.

    The char cap is checked *before* adding, so a page never exceeds it by one sentence; that
    means one long sentence can overflow on its own, which is why ``body`` is also truncated.
    """
    chars = sum(len(entry.text) for entry in current)
    if chars + len(item.text) > max_page_chars:
        return True
    elapsed = current[-1].end_ms - current[0].start_ms
    if elapsed >= target_page_ms:
        return True
    return elapsed >= seam_after_ms and item.start_ms - current[-1].end_ms >= gap_ms


def _span_ms(group: list[ScriptSentence]) -> int:
    return group[-1].end_ms - group[0].start_ms


def page_spans(pages: list[PagePlan], *, total_ms: int) -> list[tuple[PagePlan, int, int]]:
    """Each page's ``(start_ms, end_ms)`` stretch, in original time.

    A page runs until the next one starts; the last one runs to the end of the film. Doing
    it here rather than storing an end on every row means inserting a page does not require
    rewriting its neighbour.
    """
    out: list[tuple[PagePlan, int, int]] = []
    for index, page in enumerate(pages):
        end = pages[index + 1].start_ms if index + 1 < len(pages) else total_ms
        out.append((page, page.start_ms, max(page.start_ms, end)))
    return out
