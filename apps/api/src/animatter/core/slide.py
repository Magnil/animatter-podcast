"""Slide text layout: wrapping and auto-shrink, with no font or file access (§19.6).

A slide is drawn by us, not by PowerPoint, so the layout is our problem. Two decisions are
forced by the product:

* **The text must never be clipped.** A generated deck's body is transcript prose — long,
  unbroken CJK with no spaces to break at. libass taught us the cost of assuming something
  else will wrap (§19.11.9): it silently runs off the canvas. So wrapping happens here,
  before anything is drawn, and the result is a list of lines that are known to fit.
* **A page that does not fit must shrink, not overflow.** Auto-shrink is the only behaviour
  that keeps a generated 3-line title and a 9-line body readable without the user having to
  hand-tune every page.

Why the measurement is injected
------------------------------
``measure(text, size) -> width`` is a parameter rather than an import. The real one is
``ImageFont.getlength`` on a specific font file, which is deterministic but not *pure* — and
these rules (where to break, how far to shrink) are the part worth testing exhaustively.
Injecting the measurement lets the tests use a fake font where every character is 1 em, which
makes the expected layouts readable and the failures obvious.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Characters a line may not *start* with: a closing quote or a comma at the start of a line
#: reads as a typo. When a break would put one of these first, the break moves back one.
NO_LINE_START: Final[frozenset[str]] = frozenset("，。、；：！？）】》」』”’%,.;:!?)]}>")

#: Characters a line may not *end* with — the mirror case: an opening bracket or quote left
#: dangling at the end of a line, with its content starting the next one. 「比如说《」 / 「
#: BIBBIDIBA》」 was the real page that prompted this.
NO_LINE_END: Final[frozenset[str]] = frozenset("（【《「『“‘([{<")

#: Characters to break *after*, in order of preference. A break after 「，」 reads far better
#: than a break mid-phrase, and much better than a break before it.
BREAK_AFTER: Final[tuple[str, ...]] = ("。", "！", "？", "；", "：", "，", "、", " ")

#: The largest and smallest body sizes to try. Below the floor the page is unreadable, and
#: refusing is better than producing a wall of 11 px text nobody can read on a phone.
MAX_BODY_PX: Final[int] = 56
MIN_BODY_PX: Final[int] = 22
TITLE_RATIO: Final[float] = 1.6
MIN_TITLE_PX: Final[int] = 32
MAX_TITLE_PX: Final[int] = 96

#: Line spacing, as a multiple of the font size. 1.45 is loose enough for CJK (which has no
#: ascenders to speak of but a lot of ink) without wasting the page.
LEADING: Final[float] = 1.45
#: Extra gap between the title block and the body, as a multiple of the body size.
TITLE_GAP: Final[float] = 0.8

Measure = Callable[[str, int], float]


@dataclass(frozen=True)
class SlideLayout:
    """The drawing plan for one page, in device pixels relative to the page's text box."""

    title_lines: tuple[str, ...]
    body_lines: tuple[str, ...]
    title_px: int
    body_px: int
    #: Distance between baselines, in pixels, for the body. The title uses the same.
    leading_px: int
    #: Where the body's first baseline sits, measured down from the text box's top.
    body_top_px: int

    @property
    def height_px(self) -> int:
        last = self.body_top_px + max(0, len(self.body_lines) - 1) * self.leading_px
        return last + self.body_px


def wrap(text: str, *, max_width: float, measure: Callable[[str], float]) -> list[str]:
    """Break one paragraph into lines that each measure at most ``max_width``.

    CJK has no words to split on, so the break is chosen by width first and then *nudged*:

    1. take the longest prefix that fits;
    2. if a clause mark (「，」「。」…) sits in the last part of that prefix, cut right after
       it instead — a break after a comma reads far better than a break mid-phrase;
    3. if the next character would *start* the new line with a closing quote or a comma,
       pull it onto this line instead. That line then hangs past ``max_width`` by one
       character, which is invisible; a line starting with 「，」 looks like a bug.

    The earlier version wrapped by width alone and then merged punctuation-started lines
    afterwards. That merged the *whole* overflowing line into its predecessor, producing
    lines 2–3× the box width — measured on real pages, ink ran to the last pixel column.
    Nudging at the break point makes the overflow bounded by the punctuation run itself.
    """
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if not stripped:
            continue
        lines.extend(_wrap_one(stripped, max_width=max_width, measure=measure))
    return lines or [""]


def _wrap_one(text: str, *, max_width: float, measure: Callable[[str], float]) -> list[str]:
    lines: list[str] = []
    remaining = text
    while remaining:
        if measure(remaining) <= max_width:
            lines.append(remaining.rstrip())
            break
        take = _fitting_prefix(remaining, max_width=max_width, measure=measure)
        cut = _snap_out_of_latin(remaining, _preferred_cut(remaining, take))
        # Hanging punctuation: a closing mark may not start the next line, so it stays on
        # this one; an opening mark may not end the line, so it moves to the next one.
        while cut < len(remaining) and remaining[cut] in NO_LINE_START:
            cut += 1
        while cut > 1 and remaining[cut - 1] in NO_LINE_END:
            cut -= 1
        lines.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [line for line in lines if line.strip()]


def _snap_out_of_latin(text: str, cut: int) -> int:
    """Move the break out of the middle of a Latin word.

    A CJK line breaks anywhere, but 「《BI / BBIDIBA》」 reads as a typo — and the transcript is
    full of product names, which are exactly the words a reader will notice being cut. The
    break goes back to the start of the Latin run; if the run *is* the whole line (one long
    URL or English sentence), the width break stands, because moving back to 0 would loop.
    """
    if cut <= 0 or cut >= len(text):
        return cut
    if not (_is_latin(text[cut - 1]) and _is_latin(text[cut])):
        return cut
    start = cut
    while start > 0 and _is_latin(text[start - 1]):
        start -= 1
    return start if start > 0 else cut


def _is_latin(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _fitting_prefix(text: str, *, max_width: float, measure: Callable[[str], float]) -> int:
    """Length of the longest prefix of ``text`` that fits.

    Binary search: width is monotone in the prefix length, so this is exact, and it costs
    ~10 measurements per line instead of one per character — which matters because the
    shrink loop lays the page out up to 17 times.

    A single character wider than ``max_width`` still gets a line of its own: dropping it
    would be the one unacceptable outcome, since the caller can shrink but cannot recover
    lost text.
    """
    low, high = 1, len(text)
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if measure(text[:middle]) <= max_width:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _preferred_cut(text: str, take: int) -> int:
    """Move a width break to just after the latest clause mark — but only a little way.

    The window is the point. A first version searched the whole line and cut after any
    comma, which produced ragged pages: every line stopped at its first convenient comma,
    around 60 % of the measure, and the body looked like a poem. CJK prose should break at
    the measure; punctuation is a *tie-breaker* for the last few characters, not a target.

    10 % of the line (about four characters at these sizes) is enough to avoid a break in
    front of a two-character word, and small enough that the right edge stays even.
    """
    window = max(2, take // 10)
    for index in range(take - 1, max(take - window, 0) - 1, -1):
        if text[index] in BREAK_AFTER:
            return index + 1
    return take


def title_lines_for(title: str, *, max_width: float, measure: Callable[[str], float]) -> list[str]:
    """Wrap a title, returning ``[]`` when there is no title at all.

    Empty is a real layout (a section break or a quote has no title), and returning ``[""]``
    for it would reserve a whole line of vertical space for nothing — which then pushes the
    body down by one line on every such page.
    """
    if not title.strip():
        return []
    return wrap(title, max_width=max_width, measure=measure)


def layout(
    *,
    title: str,
    body: str,
    width: int,
    height: int,
    measure: Measure,
    title_measure: Measure | None = None,
    max_body_px: int = MAX_BODY_PX,
    min_body_px: int = MIN_BODY_PX,
) -> SlideLayout:
    """Pick font sizes and line breaks so the page fits in ``width×height``.

    Shrinks the body until the wrapped block fits, then sizes the title proportionally and
    shrinks it too if its own block does not fit. Returns the *smallest* size that fits,
    never smaller than ``min_body_px`` — at that point the page is over-full and the honest
    outcome is a layout that overflows slightly, not 9 px text.

    ``title_measure`` exists because the title and the body can be set in **different
    fonts**, and a font's widths are not interchangeable: 数黑 at 60 px is wider than
    微软雅黑 at 60 px, so wrapping the title with the body's measurement would produce a
    line that measures as fitting and then runs past the box when drawn. ``None`` means
    "same font for both", which is what every caller did before fonts became settable.
    """
    body_text = body.strip()
    title_text = title.strip()
    title_check = title_measure if title_measure is not None else measure

    size = max_body_px
    chosen: SlideLayout | None = None
    while size >= min_body_px:
        candidate = _try_layout(
            title=title_text, body=body_text, width=width, height=height, measure=measure,
            title_measure=title_check, body_px=size,
        )
        if candidate.height_px <= height:
            chosen = candidate
            break
        size -= 2
    if chosen is None:
        # Nothing fits: use the floor and let it overflow by a few pixels, which is visible
        # and therefore fixable. Silently dropping lines would not be.
        chosen = _try_layout(
            title=title_text, body=body_text, width=width, height=height, measure=measure,
            title_measure=title_check, body_px=min_body_px,
        )
    return chosen


def _try_layout(
    *,
    title: str,
    body: str,
    width: int,
    height: int,
    measure: Measure,
    title_measure: Measure,
    body_px: int,
) -> SlideLayout:
    body_lines = wrap(body, max_width=width, measure=_at(measure, body_px))
    body_leading = int(body_px * LEADING)

    title_px = min(MAX_TITLE_PX, max(MIN_TITLE_PX, int(body_px * TITLE_RATIO)))
    title_lines = title_lines_for(title, max_width=width, measure=_at(title_measure, title_px))
    # The title must not eat the page: if it wraps past two lines, bring it down.
    while len(title_lines) > 2 and title_px > MIN_TITLE_PX:
        title_px = max(MIN_TITLE_PX, int(title_px * 0.85))
        title_lines = title_lines_for(
            title, max_width=width, measure=_at(title_measure, title_px)
        )

    title_block = len(title_lines) * int(title_px * LEADING)
    gap = int(body_px * TITLE_GAP) if title_lines else 0
    body_top = title_block + gap

    return SlideLayout(
        title_lines=tuple(title_lines),
        body_lines=tuple(body_lines),
        title_px=title_px,
        body_px=body_px,
        leading_px=body_leading,
        body_top_px=body_top,
    )


def _at(measure: Measure, size: int) -> Callable[[str], float]:
    """Bind a measurement to one font size.

    A named helper, not a lambda in the shrink loop: ruff's B023 is right that a lambda
    closing over ``title_px`` reads it *late*, so the second iteration would measure at the
    size from the first one.
    """
    return lambda text: measure(text, size)
