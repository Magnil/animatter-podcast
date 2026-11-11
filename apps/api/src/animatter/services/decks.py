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

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from animatter.core.deck import PagePlan, page_spans, plan_pages
from animatter.core.script import Chapter, ScriptSentence
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import slide_render
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
    settings: object, project_id: str, deck_id: str, slide_id: str, *, width: int, height: int
) -> Path:
    """Where one page's PNG lives, **keyed by the size it was drawn at**.

    The size has to be part of the name. A slide is drawn at the media box's exact pixel
    size so the overlay never rescales it, but the same deck is also asked for a small
    preview by the editor — and caching by slide id alone served the 1267×712 PNG to a
    1440×810 box. The overlay then upscaled it, which is precisely the soft text this
    design exists to avoid, and the text visibly ran past the box's right edge.
    """
    return slides_dir(settings, project_id, deck_id) / f"{slide_id}-{width}x{height}.png"


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


def delete_slide(session: Session, slide_id: str) -> None:
    row = session.get(Slide, slide_id)
    if row is None:
        return
    deck_id = row.deck_id
    session.delete(row)
    session.flush()
    # Re-number so ``ord`` stays dense: a gap is harmless to the query but makes every
    # later "move up/down" off by one in a way that is invisible in the UI.
    rest = list_slides(session, deck_id)
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


def clear_slides(session: Session, deck_id: str) -> int:
    """Remove every page of a deck. The deck row survives: its id anchors the image folder."""
    rows = list_slides(session, deck_id)
    session.execute(delete(Slide).where(Slide.deck_id == deck_id))
    session.flush()
    return len(rows)


# --- generation --------------------------------------------------------


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
    """Pages from the transcript, one per topic stretch.

    ``chapters`` is optional: when the project already has ShowNotes chapters they are
    better page boundaries than the pause heuristic, so they are used as **forced** breaks.
    Without them the pause/length rules decide, which is what makes the feature work on a
    project that never generated ShowNotes.
    """
    deck = create_deck(session, project_id)
    if replace:
        clear_slides(session, deck.id)

    pages = plan_pages(sentences)
    pages = _split_at_chapters(pages, chapters or [], total_ms=total_ms)

    for page in pages:
        add_slide(
            session,
            deck,
            title=page.title,
            body=page.body,
            layout="title_body",
            source_ord=page.source_ord,
            start_ms=page.start_ms,
        )
    session.flush()
    log.info("deck.generated", project=project_id, deck=deck.id, pages=len(pages))
    view_pair = view(session, project_id)
    assert view_pair is not None
    return deck, view_pair[1]


def _split_at_chapters(
    pages: list[PagePlan], chapters: list[Chapter], *, total_ms: int
) -> list[PagePlan]:
    """Mark chapter openings as section breaks.

    Only pages that **start** a chapter become ``title`` layout pages. Re-labelling interior
    pages would be wrong: a chapter heading is not a summary of the page it lands on, it is
    the name of the stretch starting there.
    """
    if not chapters:
        return pages
    starts = {chapter.first_ord for chapter in chapters}
    out: list[PagePlan] = []
    for page in pages:
        if page.source_ord in starts:
            out.append(
                PagePlan(
                    source_ord=page.source_ord,
                    start_ms=page.start_ms,
                    last_ord=page.last_ord,
                    title=next(
                        chapter.heading
                        for chapter in chapters
                        if chapter.first_ord == page.source_ord
                    ),
                    body=page.body,
                    context=page.context,
                    speaker=page.speaker,
                )
            )
        else:
            out.append(page)
    del total_ms  # page spans are derived on read, not here
    return out


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
) -> Path:
    """Draw one page at the media box's size and return the PNG path."""
    target = image_path(settings, project_id, deck_id, row.id, width=width, height=height)
    if target.is_file():
        # Same inputs, same pixels: a re-render would only cost time. The file is keyed by
        # slide id and invalidated by `invalidate` below, never by mtime guessing.
        return target
    slide_render.render(
        target=target,
        title=row.title,
        body=row.body,
        layout_kind=row.layout,
        width=width,
        height=height,
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
) -> dict[str, Path]:
    """Render every page at one size, keyed by slide id.

    Used when the box size changes, and before an export so a missing PNG is discovered
    then — not in the middle of a filtergraph.
    """
    out: dict[str, Path] = {}
    for row in list_slides(session, deck_id):
        out[row.id] = render_slide(
            settings, project_id, deck_id, row, width=width, height=height
        )
    return out


def _session_for(settings: object) -> object:  # pragma: no cover
    raise NotImplementedError("pass a session instead: render_all is called from the service layer")


def used_spans(pages: list[PagePlan]) -> list[tuple[int, int]]:
    """Helper kept for callers that want only the numbers."""
    return [(page.start_ms, end) for page, _start, end in page_spans(pages, total_ms=0)]
