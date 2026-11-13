"""Deck and slide storage (P4, §19.6).

Two rules shape this module:

**A slide's PNG is a cache, not data.** It is regenerated from ``title``/``body``/``layout``
whenever those change, and its path is derived from the slide id — so editing text never
leaves a stale image behind under a new name, and deleting a slide deletes its picture.

**Reordering must not violate the unique index.** ``slide`` has a unique ``(deck_id, ord)``.
Swapping two rows by writing both ords one after the other collides with itself, which is why
the move path rewrites through a temporary offset instead of a direct swap.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from animatter.core.deck import PagePlan, page_spans, plan_pages, settle_pages
from animatter.core.script import Chapter, ScriptSentence
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import fonts, slide_render
from animatter.models import Deck, Slide
from animatter.services import projects as project_service

log = get_logger(__name__)

DEFAULT_TITLE = "演示稿"


@dataclass(frozen=True)
class SlideView:
    row: Slide

    def as_dict(self, project_id: str) -> dict[str, object]:
        return {
            "id": self.row.id,
            "ord": self.row.ord,
            "title": self.row.title,
            "body": self.row.body,
            "layout": self.row.layout,
            "source_ord": self.row.source_ord,
            "start_ms": self.row.start_ms,
            # Always present, even before the PNG exists: the image route draws on demand,
            # so the URL is valid the moment the row is. Handing back ``""`` here would make
            # the UI's <img> logic depend on which request happened to render first.
            "image_url": (
                f"/api/projects/{project_id}/decks/{self.row.deck_id}/slides/"
                f"{self.row.id}/image"
            ),
        }


def slides_dir(settings: object, project_id: str, deck_id: str) -> Path:
    root = project_service.project_root(settings, project_id)  # type: ignore[arg-type]
    path = root / "video" / "decks" / deck_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_path(
    settings: object,
    project_id: str,
    deck_id: str,
    slide_id: str,
    *,
    width: int,
    height: int,
    typefaces: tuple[str, str],
) -> Path:
    """Where one page's PNG lives, **keyed by the size and the typefaces it was drawn at**.

    The size has to be part of the name. A slide is drawn at the media box's exact pixel
    size so the overlay never rescales it, but the same deck is also asked for a small
    preview by the editor — and caching by slide id alone served the 1267×712 PNG to a
    1440×810 box. The overlay then upscaled it, which is precisely the soft text this
    design exists to avoid, and the text visibly ran past the box's right edge.

    The fonts are in the name for the same reason (§19.16), and ``typefaces`` is therefore
    **already resolved** — an unset deck is spelled as the family it will actually draw in,
    so "unset" and "explicitly the default" are the same file rather than two copies of the
    same pixels. A slug collision between two families is caught by ``set_fonts``, which
    empties the folder whenever the effective pair changes.
    """
    title_font, body_font = _resolve_typefaces(typefaces)
    tag = f"{_slug(title_font)}+{_slug(body_font)}"
    return slides_dir(settings, project_id, deck_id) / f"{slide_id}-{width}x{height}-{tag}.png"


def _resolve_typefaces(typefaces: tuple[str, str]) -> tuple[str, str]:
    """Map the "unset" spelling (``""``) onto the family that will actually be drawn.

    One place, so the cache key and the drawing cannot disagree: if the file name kept ``""``
    while the pixels were drawn in 微软雅黑, ``set_fonts`` would see a change on every request
    and empty the folder continuously.
    """
    return (
        typefaces[0] or slide_render.FONT_FAMILY,
        typefaces[1] or slide_render.FONT_FAMILY,
    )


def _slug(family: str) -> str:
    """A font family as a safe file-name fragment.

    Spaces and ``+`` are the two characters that matter here: the family is user data (字由's
    store has names like 「Slidefu」 and 「Alimama ShuHeiTi」), and ``+`` is already the
    separator between the two roles.
    """
    cleaned = "".join(char if char.isalnum() else "-" for char in family.strip().lower())
    slug = cleaned.strip("-")[:24]
    if not slug:
        # An all-symbol family name (not 数黑's characters, which are ``isalnum``, but a name
        # made of 「·」 and the like). A stable digest keeps the tag unique instead of
        # collapsing every such family onto the same file name — and stable across processes,
        # which ``hash()`` is not.
        digest = hashlib.sha256(family.encode("utf-8")).hexdigest()[:8]
        slug = f"x{digest}"
    return slug


# --- reading -----------------------------------------------------------


def get_deck(session: Session, project_id: str) -> Deck | None:
    return session.scalars(
        select(Deck).where(Deck.project_id == project_id).order_by(Deck.created_at.desc())
    ).first()


def require_deck(session: Session, project_id: str) -> Deck:
    row = get_deck(session, project_id)
    if row is None:
        raise AppError(
            "deck_missing",
            "这个项目还没有演示稿",
            hint="先在「演示稿」里从字幕生成一份。",
            status=404,
        )
    return row


def get_deck_by_id(session: Session, deck_id: str) -> Deck | None:
    return session.get(Deck, deck_id)


def deck_fonts(deck: Deck) -> tuple[str, str]:
    """The ``(title_font, body_font)`` this deck draws with, defaults resolved.

    Empty means "unset" on the row, and the renderer's default is what an unset deck gets.
    Resolving here rather than in the router keeps one definition of the default: the image
    route, the film and the panel all read the same pair.
    """
    return (
        deck.title_font or slide_render.FONT_FAMILY,
        deck.body_font or slide_render.FONT_FAMILY,
    )


def set_fonts(
    session: Session,
    settings: object,
    deck: Deck,
    *,
    title_font: str | None = None,
    body_font: str | None = None,
) -> Deck:
    """Change the deck's typefaces and throw away every stale PNG.

    The pictures *must* go, at every size. The file name carries the pair (§19.16), so a font
    change already lands on a new path — but the *old* files would then be unreachable garbage
    that ``purge_images`` is the only thing to collect, and a page whose font was changed back
    would be served from a stale picture. Trap 24 in §19.11 is the same failure one level up:
    an unchanged URL showing old pixels.

    ``settings`` is explicit for the same reason ``delete_slide``'s is: the PNG lives on disk
    and the row does not know where, so every caller must be able to say.

    ``None`` means "leave as is"; the empty string means "back to the default", which is why
    the argument that clears is not the same as the argument that is absent.

    The purge is keyed on the **effective** pair, not on the stored strings: picking the
    default family explicitly from the picker changes no pixel, and dropping 30 pages' worth
    of PNGs (and re-drawing them) for a no-op is exactly the kind of hidden cost that makes a
    UI feel slow.
    """
    before = deck_fonts(deck)
    if title_font is not None:
        deck.title_font = _checked_font(title_font)
    if body_font is not None:
        deck.body_font = _checked_font(body_font)
    if deck_fonts(deck) == before:
        return deck
    session.flush()
    purge_images(session, settings, deck.id, None)
    log.info(
        "deck.fonts",
        deck=deck.id,
        title=deck.title_font or "-",
        body=deck.body_font or "-",
    )
    return deck


def _checked_font(family: str) -> str:
    """Reject a family this machine cannot draw, *before* the row is written.

    Accepting it would store a deck that 503s on every preview and every export, with the
    error only surfacing later on a render path that has no way to ask the user to choose
    again. The picker only offers drawable families, so this is a guard against a stale UI or
    a hand-made request, not against the normal flow.
    """
    name = family.strip()
    if not name:
        return ""
    if not fonts.draws_chinese(name):
        available = "、".join(fonts.chinese_families()[:8]) or "（一个都没找到）"
        raise AppError(
            "slide_font_unusable",
            f"「{name}」画不了中文",
            hint=f"换一个能画中文的字体，这台机器上有：{available}…",
        )
    return name


def get_slide(session: Session, slide_id: str) -> Slide | None:
    return session.get(Slide, slide_id)


def list_slides(session: Session, deck_id: str) -> list[Slide]:
    return list(
        session.scalars(
            select(Slide).where(Slide.deck_id == deck_id).order_by(Slide.ord)
        ).all()
    )


def view(session: Session, project_id: str) -> tuple[Deck, list[SlideView]] | None:
    deck = get_deck(session, project_id)
    if deck is None:
        return None
    rows = list_slides(session, deck.id)
    return deck, [SlideView(row=row) for row in rows]


# --- writing -----------------------------------------------------------


def create_deck(session: Session, project_id: str, *, title: str = "") -> Deck:
    """One deck per project. Re-creating returns the existing one instead of a second deck.

    A project with two decks would need a chooser on every screen and a rule for which one
    the box plays; neither is worth it, and 「重新生成」 is the operation people actually want.
    """
    existing = get_deck(session, project_id)
    if existing is not None:
        return existing
    row = Deck(
        id=new_id("deck"),
        project_id=project_id,
        title=title.strip() or DEFAULT_TITLE,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    log.info("deck.created", project=project_id, deck=row.id)
    return row


def add_slide(
    session: Session,
    deck: Deck,
    *,
    title: str,
    body: str,
    layout: str = "title_body",
    source_ord: int = -1,
    start_ms: int = 0,
) -> Slide:
    next_ord = len(list_slides(session, deck.id))
    row = Slide(
        id=new_id("sld"),
        deck_id=deck.id,
        ord=next_ord,
        title=title,
        body=body,
        layout=layout,
        source_ord=source_ord,
        start_ms=start_ms,
    )
    session.add(row)
    session.flush()
    return row


def update_slide(
    session: Session,
    slide_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    layout: str | None = None,
    start_ms: int | None = None,
) -> Slide:
    row = session.get(Slide, slide_id)
    if row is None:
        raise AppError("slide_missing", "这一页不存在", status=404)
    if title is not None:
        row.title = title
    if body is not None:
        row.body = body
    if layout is not None:
        if layout not in slide_render.LAYOUTS:
            raise AppError(
                "slide_layout_invalid",
                f"未知的版式 {layout}",
                hint=f"可选：{'、'.join(slide_render.LAYOUTS)}。",
            )
        row.layout = layout
    if start_ms is not None:
        row.start_ms = max(0, int(start_ms))
    session.flush()
    return row


def delete_slide(session: Session, settings: object, slide_id: str) -> None:
    """Remove one page, its picture, and re-densify ``ord``.

    ``settings`` is a required argument, not an optional one. The PNG lives on disk and the
    row does not know where — so every caller must be able to say. Leaving it optional meant
    the natural call site forgot it, and a regenerate-then-preview cycle leaked 62 orphaned
    PNGs (14 MB) on one 60-page deck before anyone looked at the folder.
    """
    row = session.get(Slide, slide_id)
    if row is None:
        return
    deck_id = row.deck_id
    session.delete(row)
    session.flush()
    purge_images(session, settings, deck_id, [slide_id])
    # Re-number so ``ord`` stays dense: a gap is harmless to the query but makes every
    # later "move up/down" off by one in a way that is invisible in the UI.
    #
    # Parked first, like ``move_slide``, for the same reason: ``(deck_id, ord)`` is unique and
    # a single flush applies the updates in an arbitrary order, so renumbering rows at 1 and 2
    # down to 0 and 1 can run 2→1 first and collide with the row still at 1. Measured in the
    # full suite as ``UNIQUE constraint failed: slide.deck_id, slide.ord`` — and it only
    # reproduces when the deck *already* had a gap, which is why it took a whole-suite run.
    rest = list_slides(session, deck_id)
    # ``max(...) + 1``, not ``len(rest)``: a deck that already had a gap can hold the ords
    # 1 and 2 with only two rows, and parking at ``len + index`` would then land on a slot
    # that is still occupied. Measured — same constraint, different parameters.
    park = max((item.ord for item in rest), default=-1) + 1
    for index, item in enumerate(rest):
        if item.ord != index:
            item.ord = park + index
    session.flush()
    for index, item in enumerate(rest):
        if item.ord != index:
            item.ord = index
    session.flush()


def move_slide(session: Session, slide_id: str, *, to_ord: int) -> list[Slide]:
    """Move one slide to a position, shifting the rest.

    The unique ``(deck_id, ord)`` index is why this goes through a gap: assigning directly
    would collide with the row that currently holds the target position.
    """
    row = session.get(Slide, slide_id)
    if row is None:
        raise AppError("slide_missing", "这一页不存在", status=404)
    rows = list_slides(session, row.deck_id)
    target = max(0, min(len(rows) - 1, int(to_ord)))
    if row.ord == target:
        return rows

    ordered = [item for item in rows if item.id != row.id]
    ordered.insert(target, row)
    # Park every row beyond the end first, so no intermediate state is a duplicate.
    for index, item in enumerate(ordered):
        item.ord = len(ordered) + index
    session.flush()
    for index, item in enumerate(ordered):
        item.ord = index
    session.flush()
    return list_slides(session, row.deck_id)


def clear_slides(session: Session, settings: object, deck_id: str) -> int:
    """Remove every page of a deck *and* every picture in its folder.

    The deck row survives: its id anchors the image folder, and deleting it would orphan the
    whole directory with nothing left to say where it was.

    Dropping the rows without the files is what 「重新生成」 did — measured 62 orphaned PNGs
    / 14 MB on a single 60-page deck after one regenerate. Every page id is different after
    a regenerate, so the old pictures can never be reached again; they are garbage the
    moment the rows go.

    **The whole directory, not just the ids being deleted.** A per-id purge looks right and
    leaves the garbage that an *earlier* buggy regenerate already dropped: those ids are no
    longer in any row, so nothing can name them again. This folder belongs to exactly one
    deck, and "clear every page" means precisely "empty it" — the id-driven purge is only
    needed for a single-page delete, where the other pages' pictures must survive.
    """
    rows = list_slides(session, deck_id)
    session.execute(delete(Slide).where(Slide.deck_id == deck_id))
    session.flush()
    purge_images(session, settings, deck_id, None)
    return len(rows)


def purge_images(
    session: Session, settings: object, deck_id: str, slide_ids: list[str] | None
) -> int:
    """Delete cached pictures. Returns how many files went.

    ``slide_ids=None`` means the whole folder (used when every page is being replaced);
    a list means only those pages' files, at every cached size — a page is drawn at the
    editor's preview size *and* at the media box's size (``{id}-{w}x{h}.png``), and clearing
    only one would leave the other showing the previous text.

    The directory is computed without ``slides_dir`` on purpose — that helper creates the
    folder, and a purge that creates the folder it is told to empty is a trap for the next
    reader.
    """
    deck = session.get(Deck, deck_id)
    if deck is None:
        return 0
    directory = (
        project_service.project_root(settings, deck.project_id)  # type: ignore[arg-type]
        / "video"
        / "decks"
        / deck_id
    )
    if not directory.is_dir():
        return 0
    patterns = [f"{slide_id}-*.png" for slide_id in slide_ids] if slide_ids else ["*.png"]
    removed = 0
    for pattern in patterns:
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


# --- generation --------------------------------------------------------


def generate_from_pages(
    session: Session,
    settings: object,
    project_id: str,
    *,
    pages: list[PagePlan],
    replace: bool = True,
    source: str = "transcript",
    warnings: list[str] | None = None,
) -> tuple[Deck, list[SlideView]]:
    """Write already-settled pages as rows. The only place a page becomes a row.

    ``source`` and ``warnings`` are recorded on the deck rather than returned to the caller:
    the panel re-reads the deck on every mount and after every edit, so anything it has to
    show about *how* the pages were made has to survive a round trip through the database.
    """
    deck = create_deck(session, project_id)
    if replace:
        clear_slides(session, settings, deck.id)

    for page in pages:
        add_slide(
            session,
            deck,
            title=page.title,
            body=page.body,
            layout=page.layout,
            source_ord=page.source_ord,
            start_ms=page.start_ms,
        )
    record_provenance(deck, source=source, warnings=warnings or [])
    session.flush()
    log.info("deck.generated", project=project_id, deck=deck.id, pages=len(pages), source=source)
    view_pair = view(session, project_id)
    assert view_pair is not None
    return deck, view_pair[1]


def record_provenance(deck: Deck, *, source: str, warnings: list[str]) -> None:
    """Store how this deck was made. Called on every generate, including a failed outline.

    Written even when nothing went wrong: ``source`` is what stops a fallback deck from
    quietly posing as a planned one, and clearing the warnings on a clean run is the only way
    the previous run's complaint ever disappears.
    """
    deck.source = source
    deck.warnings_json = json.dumps(warnings, ensure_ascii=False)


def stored_warnings(deck: Deck) -> list[str]:
    """The last run's warnings, tolerating a row written by an older version."""
    try:
        value = json.loads(deck.warnings_json or "[]")
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def generate_from_transcript(
    session: Session,
    settings: object,
    project_id: str,
    *,
    sentences: list[ScriptSentence],
    chapters: list[Chapter] | None = None,
    total_ms: int,
    replace: bool = True,
) -> tuple[Deck, list[SlideView]]:
    """The **fallback** planner: pages cut at pauses, body = the transcript itself.

    Kept as the path for a project with no API key or a failed outline call, and as the one
    path a test can drive without a model. Its output is honest and complete and is not what
    the feature is for — see :func:`animatter.services.deck_outline.outline_pages` for that.

    ``chapters`` are still applied, through the same :func:`settle_pages` the model path uses,
    so a chapter heading wins over a first-clause title on either path.
    """
    settled, _warnings = settle_pages(
        plan_pages(sentences),
        sentences=sentences,
        total_ms=total_ms,
        chapters=chapters or [],
    )
    return generate_from_pages(session, settings, project_id, pages=settled, replace=replace)


def spans_of(rows: list[Slide], *, total_ms: int) -> list[tuple[Slide, int, int]]:
    """Each slide's ``(start_ms, end_ms)`` in original time — until the next one starts."""
    out: list[tuple[Slide, int, int]] = []
    for index, row in enumerate(rows):
        end = rows[index + 1].start_ms if index + 1 < len(rows) else total_ms
        out.append((row, row.start_ms, max(row.start_ms, end)))
    return out


# --- rendering ---------------------------------------------------------


def render_slide(
    settings: object,
    project_id: str,
    deck_id: str,
    row: Slide,
    *,
    width: int,
    height: int,
    typefaces: tuple[str, str],
) -> Path:
    """Draw one page at the media box's size and return the PNG path.

    ``typefaces`` is ``(title_font, body_font)`` and is **required**, with no default. A
    default here would be the exact bug this round is about: every caller that forgot it
    would draw the page in the renderer's default and the cache would happily serve it, so a
    deck set in 数黑 would silently export in 微软雅黑 and nothing would look wrong until
    someone compared the preview with the film.
    """
    target = image_path(
        settings, project_id, deck_id, row.id, width=width, height=height, typefaces=typefaces
    )
    if target.is_file():
        # Same inputs, same pixels: a re-render would only cost time. The file is keyed by
        # slide id, size and typefaces, and invalidated by `invalidate` / `set_fonts`.
        return target
    title_font, body_font = _resolve_typefaces(typefaces)
    slide_render.render(
        target=target,
        title=row.title,
        body=row.body,
        layout_kind=row.layout,
        width=width,
        height=height,
        title_font=title_font,
        body_font=body_font,
    )
    return target


def invalidate(settings: object, project_id: str, deck_id: str, slide_id: str) -> None:
    """Delete every size of this page's PNG.

    Every size, not the one the last request happened to use: the editor previews at one
    size and the film draws at another, and leaving the other one behind would keep showing
    the previous text there.
    """
    for path in slides_dir(settings, project_id, deck_id).glob(f"{slide_id}-*.png"):
        path.unlink(missing_ok=True)


def render_all(
    session: Session,
    settings: object,
    project_id: str,
    deck_id: str,
    *,
    width: int,
    height: int,
    typefaces: tuple[str, str],
) -> dict[str, Path]:
    """Render every page at one size, keyed by slide id.

    Used when the box size changes, and before an export so a missing PNG is discovered
    then — not in the middle of a filtergraph.
    """
    out: dict[str, Path] = {}
    for row in list_slides(session, deck_id):
        out[row.id] = render_slide(
            settings, project_id, deck_id, row, width=width, height=height, typefaces=typefaces
        )
    return out


def _session_for(settings: object) -> object:  # pragma: no cover
    raise NotImplementedError("pass a session instead: render_all is called from the service layer")


def used_spans(pages: list[PagePlan]) -> list[tuple[int, int]]:
    """Helper kept for callers that want only the numbers."""
    return [(page.start_ms, end) for page, _start, end in page_spans(pages, total_ms=0)]
