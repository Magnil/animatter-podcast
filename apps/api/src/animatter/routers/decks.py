"""Deck and slide routes (P4, §19.6).

Shapes, and why:

* the deck is **one per project**, so there is no id in the collection URL — the UI asks
  "this project's deck" and the server answers, or ``null``;
* ``image_url`` is returned by the server rather than built by the client, for the same
  reason the template asset URL is (§19.11.11): a URL the client assembles can look valid
  and 404, and a test that fetches the URL the response hands back catches that.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.services import clipping as clipping_service
from animatter.services import decks as deck_service
from animatter.services import script as script_service

router = APIRouter(prefix="/api", tags=["decks"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db = request.app.state.db
    with db.session() as session:
        yield session


class SlideOut(BaseModel):
    id: str
    ord: int
    title: str
    body: str
    layout: str
    source_ord: int
    start_ms: int
    image_url: str


class DeckOut(BaseModel):
    id: str
    project_id: str
    title: str
    slides: list[SlideOut]


class ChapterIn(BaseModel):
    heading: str
    start_ms: int


class GenerateIn(BaseModel):
    #: ``True`` replaces the existing pages, which is what 「重新生成」 means.
    replace: bool = True
    #: Chapter openings to use as section breaks. Empty means "let the pause rule decide".
    chapters: list[ChapterIn] = Field(default_factory=list)


class SlidePatchIn(BaseModel):
    title: str | None = None
    body: str | None = None
    layout: str | None = None
    start_ms: int | None = None


class SlideMoveIn(BaseModel):
    to_ord: int = Field(ge=0)


def _out(deck: object, slides: list[deck_service.SlideView], project_id: str) -> DeckOut:
    return DeckOut(
        id=deck.id,  # type: ignore[attr-defined]
        project_id=project_id,
        title=deck.title,  # type: ignore[attr-defined]
        slides=[SlideOut(**item.as_dict(project_id)) for item in slides],  # type: ignore[arg-type]
    )


@router.get("/projects/{project_id}/deck", response_model=DeckOut | None)
def get_deck(
    project_id: str, request: Request, session: Session = Depends(_session_dependency)
) -> DeckOut | None:
    """The project's deck, or ``null``.

    ``null`` rather than 404: "no deck yet" is the normal starting state, and a 404 would
    light up the console on every page load before the user has touched the feature.
    """
    pair = deck_service.view(session, project_id)
    if pair is None:
        return None
    deck, slides = pair
    return _out(deck, slides, project_id)


@router.post("/versions/{version_id}/deck/generate", response_model=DeckOut, status_code=201)
def generate_deck(
    version_id: str,
    payload: GenerateIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> DeckOut:
    """Pages from the transcript — no LLM call, so this is instant and free.

    ``chapters`` is passed in by the caller rather than read from storage: chapters are not
    persisted (the ShowNotes generator returns them and they are rendered straight into
    markdown), so the only place they exist is the response the UI already has in hand.
    """
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    sentences = script_service.script_sentences(session, settings, version)
    chapters = script_service.chapters_from_pairs(
        [(item.heading, item.start_ms) for item in payload.chapters], session, version
    )

    deck, slides = deck_service.generate_from_transcript(
        session,
        settings,
        version.project_id,
        sentences=sentences,
        chapters=chapters,
        total_ms=version.duration_ms,
        replace=payload.replace,
    )
    session.commit()
    return _out(deck, slides, version.project_id)


@router.patch("/slides/{slide_id}", response_model=SlideOut)
def patch_slide(
    slide_id: str,
    payload: SlidePatchIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> SlideOut:
    """Edit one page. The PNG is invalidated so the next view redraws it.

    Invalidating here rather than trusting an mtime comparison is what keeps
    「改了文字 → 预览还是旧的」 from ever happening.
    """
    settings: Settings = request.app.state.settings
    row = deck_service.update_slide(
        session,
        slide_id,
        title=payload.title,
        body=payload.body,
        layout=payload.layout,
        start_ms=payload.start_ms,
    )
    deck = deck_service.get_deck_by_id(session, row.deck_id)
    if deck is None:
        raise AppError("deck_missing", "演示稿不存在", status=404)
    deck_service.invalidate(settings, deck.project_id, deck.id, row.id)
    session.commit()
    return SlideOut(
        id=row.id,
        ord=row.ord,
        title=row.title,
        body=row.body,
        layout=row.layout,
        source_ord=row.source_ord,
        start_ms=row.start_ms,
        image_url="",
    )


@router.post("/slides/{slide_id}/move", response_model=list[SlideOut])
def move_slide(
    slide_id: str,
    payload: SlideMoveIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> list[SlideOut]:
    row = deck_service.get_slide(session, slide_id)
    if row is None:
        raise AppError("slide_missing", "这一页不存在", status=404)
    deck = deck_service.get_deck_by_id(session, row.deck_id)
    if deck is None:
        raise AppError("deck_missing", "演示稿不存在", status=404)

    deck_service.move_slide(session, slide_id, to_ord=payload.to_ord)
    session.commit()
    pair = deck_service.view(session, deck.project_id)
    if pair is None:
        return []
    return [SlideOut(**item.as_dict(project_id=deck.project_id)) for item in pair[1]]  # type: ignore[arg-type]


@router.delete("/slides/{slide_id}", status_code=204)
def delete_slide(slide_id: str, session: Session = Depends(_session_dependency)) -> None:
    deck_service.delete_slide(session, slide_id)
    session.commit()


@router.get("/projects/{project_id}/decks/{deck_id}/slides/{slide_id}/image")
def slide_image(
    project_id: str,
    deck_id: str,
    slide_id: str,
    request: Request,
    width: int = Query(default=0, ge=0, le=3840),
    height: int = Query(default=0, ge=0, le=2160),
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    """The page's PNG, drawn on demand at the requested size.

    On demand rather than at edit time because the size depends on the media box, which the
    deck service does not know about. Rendering is ~30 ms and idempotent, so asking twice
    costs nothing.
    """
    settings: Settings = request.app.state.settings
    row = deck_service.get_slide(session, slide_id)
    if row is None or row.deck_id != deck_id:
        raise AppError("slide_missing", "这一页不存在", status=404)
    path = deck_service.render_slide(
        settings,
        project_id,
        deck_id,
        row,
        width=width or 1440,
        height=height or 810,
    )
    return FileResponse(path, media_type="image/png")
