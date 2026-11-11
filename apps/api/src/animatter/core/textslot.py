"""Which PSD text layers become editable slots, and what they default to (§19.12).

The contract from §19.2 is that text is **drawn by us**, never read back out of the PSD. That
leaves one question this module answers: given the visible type layers, which of them is the
episode number, which is the episode title, which are avatar names, and what does each one
start out looking like?

Classification is geometry plus one pattern match, deliberately in that order:

* a type layer **inside an avatar group** is that slot's name — the group is the address;
* otherwise a layer in the **header band** (top of the canvas, right half) is either the
  episode number or the title. The number is identified by its *text* (``EP.23``,
  ``第12期``, ``S02E07``) because size does not separate them on a real template — measured
  on the sample, the episode number is **larger** (91.7 px) than the title (75 px), so any
  "bigger one is the title" rule gets it backwards;
* everything else is a **note**: still editable, just not part of the title block.

``note`` is not a dumping ground we forget about. The sample's footer
(「欢迎大家关注、加群、一键三连！」) is one, and leaving it flattened into the background would
mean the user can edit the title but not the line right under the box.

Pure module: no IO, no PIL. Sizes and colours are computed here so they can be asserted
without rendering anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from animatter.core import textspan

#: Roles, in the order the UI groups them.
ROLE_EPISODE: Final[str] = "episode"
ROLE_TITLE: Final[str] = "title"
ROLE_AVATAR_NAME: Final[str] = "avatar_name"
ROLE_NOTE: Final[str] = "note"
#: The designer's placeholder text **inside the content rect**. Never drawn: the footage is
#: what belongs there. Kept as a slot so the UI can list it as 「框内文字，暂不参与渲染」 rather
#: than leaving the user to wonder why the PSD's line vanished.
ROLE_CONTENT: Final[str] = "content"

#: Anything matching one of these is the episode number. Covers the spellings a podcast
#: template actually uses: ``EP.23`` / ``Ep 23`` / ``第12期`` / ``S02E07`` / ``Vol.5``.
_EPISODE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*(?:ep|vol|no)\s*[.:#\-\s]?\s*\d+", re.IGNORECASE),
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百]+\s*[期话集]"),
    re.compile(r"^\s*s\d+\s*e\d+", re.IGNORECASE),
)

#: A header-band layer must sit in the right half of the canvas and above this fraction of
#: its height. The sample's two header texts are at y 20–86 and 117–262 of 1440, so both fit
#: comfortably; the box's own text (y 523+) does not, and neither does the footer (y 1374).
_HEADER_BAND: Final[float] = 0.35
_HEADER_RIGHT_HALF: Final[float] = 0.5


@dataclass(frozen=True)
class Box:
    """A rectangle in PSD canvas coordinates (what the caller measured)."""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w


@dataclass(frozen=True)
class Candidate:
    """One visible type layer, already reduced to what classification needs.

    ``font_size`` is the **effective** size in canvas pixels: the PSD's own size multiplied
    by the layer's transform scale. Those differ on this template — avatar names carry a
    0.849 transform, so their 25 px is really 21.2 px — and using the untransformed number
    makes every avatar name render a fifth too large.
    """

    name: str
    text: str
    box: Box
    font_size: float
    colour: str
    #: Name of the avatar group this layer sits in, when it does.
    avatar: str | None = None


@dataclass(frozen=True)
class Slot:
    """A classified text slot with its stable key."""

    key: str
    role: str
    text: str
    box: Box
    font_size: float
    colour: str
    #: Human label for the UI: 「期号」/「当期标题」/「主播1」/「页脚」.
    label: str
    #: The PSD layer (or avatar group) this came from. Kept for diagnostics and for matching a
    #: slot back to its layer — never shown to the user, because Photoshop names an un-renamed
    #: text layer after the text typed into it.
    name: str = ""


def is_episode_number(text: str) -> bool:
    """Whether this reads as an episode number, **ignoring colour markup**.

    ``EP.**23**`` is the same episode number as ``EP.23``; the markers are presentation, and a
    pattern that saw them would fail to match — which is exactly how the episode slot and the
    title slot swapped places the first time the markup was added.
    """
    stripped = textspan.plain(text).strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in _EPISODE_PATTERNS)


def in_header(box: Box, *, canvas_w: int, canvas_h: int) -> bool:
    return box.y + box.h <= canvas_h * _HEADER_BAND and box.x >= canvas_w * _HEADER_RIGHT_HALF


def _inside(box: Box, content: Box) -> bool:
    """Whether ``box`` is mostly inside ``content`` — a *majority* test, not full containment.

    A placeholder that overhangs the content rect by a few pixels is still a placeholder. A
    third of its area is the threshold: at half, a text block straddling the box's edge would
    flip roles on a two-pixel layout change.
    """
    left = max(box.x, content.x)
    top = max(box.y, content.y)
    right = min(box.right, content.right)
    bottom = min(box.y + box.h, content.y + content.h)
    if right <= left or bottom <= top:
        return False
    overlap = (right - left) * (bottom - top)
    return overlap * 3 >= box.w * box.h


#: The band below the content rect, as a fraction of the canvas height. A note down there is the
#: footer: the sample's sits at y=1031 of 1080 while the box ends at y=767.
_FOOTER_BAND: Final[float] = 0.9


def classify(
    candidates: list[Candidate],
    *,
    canvas_w: int,
    canvas_h: int,
    content: Box | None = None,
    avatar_labels: dict[str, str] | None = None,
) -> list[Slot]:
    """Turn raw type layers into slots with stable keys.

    Keys are what the user's overrides are stored under, so they have to survive a
    recompile: they are derived from **role** and the PSD's own layer/group name, never from
    an index that compiles can shuffle.

    ``content`` is the media box, when the caller knows it. Anything inside it becomes
    :data:`ROLE_CONTENT` and is left out of the overlay — see the module docstring.

    ``avatar_labels`` maps an avatar group's PSD name (``头像1 拷贝``) to what the user calls it
    (``主播1``). Labels are what the form shows; keys stay on the PSD's own names so a user's
    overrides survive a recompile even if the side logic is ever retuned.
    """
    header: list[Candidate] = []
    others: list[Candidate] = []
    names: list[Candidate] = []

    for item in candidates:
        if item.avatar is not None:
            names.append(item)
        elif in_header(item.box, canvas_w=canvas_w, canvas_h=canvas_h):
            header.append(item)
        else:
            others.append(item)

    # --- episode number and title -------------------------------------
    episode = next((item for item in header if is_episode_number(item.text)), None)
    rest = [item for item in header if item is not episode]
    # The title is the largest remaining header layer. With no pattern match at all (a
    # template that writes 「最新一期」 instead of a number), the *smallest* header layer is
    # taken as the number and the largest as the title — arbitrary, but it keeps both
    # editable instead of silently folding one into the background.
    title = max(rest, key=lambda item: item.font_size) if rest else None
    if episode is None and len(rest) >= 2:
        episode = min(rest, key=lambda item: item.font_size)
        title = max(rest, key=lambda item: item.font_size)

    slots: list[Slot] = []
    if episode is not None:
        slots.append(
            Slot(
                key=ROLE_EPISODE,
                role=ROLE_EPISODE,
                text=episode.text,
                box=episode.box,
                font_size=episode.font_size,
                colour=episode.colour,
                label="期号",
                name=episode.name,
            )
        )
    if title is not None:
        slots.append(
            Slot(
                key=ROLE_TITLE,
                role=ROLE_TITLE,
                text=title.text,
                box=title.box,
                font_size=title.font_size,
                colour=title.colour,
                label="当期标题",
                name=title.name,
            )
        )

    # Hosts first, then guests, each top-down. Sorting by geometry alone interleaved the two
    # columns (主播1, 嘉宾1, 主播2, 嘉宾2); the user reads them as two groups.
    def _name_order(candidate: Candidate) -> tuple[int, int, int]:
        shown = (avatar_labels or {}).get(candidate.avatar or "", "")
        side = 0 if shown.startswith("主播") else 1
        digits = "".join(char for char in shown if char.isdigit()) or "0"
        return (side, int(digits), candidate.box.y)

    for item in sorted(names, key=_name_order):
        assert item.avatar is not None  # narrowed by the branch above
        shown = (avatar_labels or {}).get(item.avatar, item.avatar)
        slots.append(
            Slot(
                key=f"avatar:{item.avatar}",
                role=ROLE_AVATAR_NAME,
                text=item.text,
                box=item.box,
                font_size=item.font_size,
                colour=item.colour,
                label=shown,
                name=item.name,
            )
        )

    plain_notes: list[Candidate] = []
    for item in sorted(others, key=lambda candidate: (candidate.box.y, candidate.box.x)):
        if content is not None and _inside(item.box, content):
            slots.append(
                Slot(
                    key=f"note:{item.name}",
                    role=ROLE_CONTENT,
                    text=item.text,
                    box=item.box,
                    font_size=item.font_size,
                    colour=item.colour,
                    # Still its layer name: these are the designer's placeholders and the user
                    # is being told why they are not editable, so the PSD's own name is the
                    # useful thing to show.
                    label=item.name or "框内文字",
                    name=item.name,
                )
            )
        else:
            plain_notes.append(item)

    # Notes get a **role** name, not their own text. `label = layer.name` was the bug behind
    # 「页脚」 showing up as 「欢迎大家关注、加群、一键三连！ 详情请见视频简介」: Photoshop names an
    # un-renamed text layer after what was typed into it, so the form field was labelled with
    # the value it contains — and the label went stale the moment the value changed.
    footer_count = 0
    note_count = 0
    for item in plain_notes:
        is_footer = content is None or item.box.y >= canvas_h * _FOOTER_BAND
        if is_footer:
            footer_count += 1
            label = "页脚" if footer_count == 1 else f"页脚{footer_count}"
        else:
            note_count += 1
            label = "其他文字" if note_count == 1 else f"其他文字{note_count}"
        slots.append(
            Slot(
                key=f"note:{item.name}",
                role=ROLE_NOTE,
                text=item.text,
                box=item.box,
                font_size=item.font_size,
                colour=item.colour,
                label=label,
                name=item.name,
            )
        )
    return slots


def colour_from_values(values: list[float] | None) -> str:
    """``#rrggbb`` from Photoshop's ``[alpha, r, g, b]`` fill values.

    Missing fill means black, which is what Photoshop uses when the layer inherits its colour
    — the sample's title layer has no ``FillColor`` at all and is indeed near-black.
    """
    if not values or len(values) < 4:
        return "#000000"
    _alpha, red, green, blue = values[:4]
    return "#{:02x}{:02x}{:02x}".format(
        *(
            max(0, min(255, round(channel * 255)))
            for channel in (float(red), float(green), float(blue))
        )
    )


def scaled_size(font_size: float, transform_scale: float, factor: float) -> int:
    """Effective pixels at the render canvas: PSD size × layer scale × canvas factor.

    Rounded, minimum 6 px. A 6 px floor is not cosmetic: a template with a 10 px caption in a
    2560-wide PSD is 7 px at 1920, and rounding it to 0 would make the slot invisible while
    still being listed as editable.
    """
    value = float(font_size) * float(transform_scale or 1.0) * float(factor)
    return max(6, round(value))
