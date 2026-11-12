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
from animatter.core.deck import PagePlan, plan_pages, settle_pages
from animatter.errors import AppError
from animatter.media import fonts
from animatter.providers.http import build_client
from animatter.services import clipping as clipping_service
from animatter.services import deck_outline
from animatter.services import decks as deck_service
from animatter.services import projects as project_service
from animatter.services import script as script_service
from animatter.services.llm import build_llm

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
    #: ``model`` when the pages came from an outline call, ``transcript`` when the fallback
    #: planner stood in. The panel says which, because "why is my deck a transcript" is the
    #: one question this feature has to be able to answer about itself.
    source: str = "transcript"
    #: Non-fatal problems with the generated pages, already in user-facing Chinese.
    warnings: list[str] = Field(default_factory=list)
    #: The typefaces the pages are drawn with, **already resolved** (an unset deck reports the
    #: renderer's default rather than ``""``). The panel's pickers bind to these, and a picker
    #: whose value is the empty string would show a blank option instead of the face the
    #: preview is actually in.
    title_font: str = ""
    body_font: str = ""


class ChapterIn(BaseModel):
    heading: str
    start_ms: int


class GenerateIn(BaseModel):
    #: ``True`` replaces the existing pages, which is what 「重新生成」 means.
    replace: bool = True
    #: Chapter openings to use as section breaks. Empty means "let the planner decide".
    chapters: list[ChapterIn] = Field(default_factory=list)
    #: ``plan`` (default) asks the model where the topics turn; ``transcript`` cuts the
    #: transcript at pauses. The second one exists because a project may have no API key and
    #: because re-generating a 60-page deck for free is a legitimate thing to want.
    mode: str = Field(default="plan", description="plan | transcript")


class SlidePatchIn(BaseModel):
    title: str | None = None
    body: str | None = None
    layout: str | None = None
    start_ms: int | None = None


class SlideMoveIn(BaseModel):
    to_ord: int = Field(ge=0)


class DeckFontsIn(BaseModel):
    """Set the deck's typefaces. Absent means "leave it", ``""`` means "back to default"."""

    title_font: str | None = Field(default=None, max_length=120)
    body_font: str | None = Field(default=None, max_length=120)


class FontListOut(BaseModel):
    #: Families that can draw Chinese — what the deck's pickers offer. The template tab uses
    #: the wider ``/api/fonts`` list, which also contains the design's Latin-only faces.
    families: list[str]
    #: The current pair, resolved, so the pickers can show it even if it is not in ``families``.
    title_font: str
    body_font: str


def _out(
    deck: object,
    slides: list[deck_service.SlideView],
    project_id: str,
    *,
    warnings: list[str] | None = None,
) -> DeckOut:
    row = deck
    title_font, body_font = deck_service.deck_fonts(row)  # type: ignore[arg-type]
    return DeckOut(
        id=row.id,  # type: ignore[attr-defined]
        project_id=project_id,
        title=row.title,  # type: ignore[attr-defined]
        slides=[SlideOut(**item.as_dict(project_id)) for item in slides],  # type: ignore[arg-type]
        source=str(row.source),  # type: ignore[attr-defined]
        warnings=(
            warnings
            if warnings is not None
            else deck_service.stored_warnings(row)  # type: ignore[arg-type]
        ),
        title_font=title_font,
        body_font=body_font,
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


@router.patch("/projects/{project_id}/deck", response_model=DeckOut)
def patch_deck_fonts(
    project_id: str,
    payload: DeckFontsIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> DeckOut:
    """Set the deck's title and body typefaces, independently.

    ``PATCH`` on the deck rather than on a slide: type is deck-wide, the way a presentation's
    master is. A per-slide font would make the deck inconsistent page to page, which is the one
    thing a slide deck must not be.

    Every PNG is dropped as a side effect (``deck_service.set_fonts``) — the file name does
    not contain the fonts the caller just chose unless the deck is re-rendered, and a stale
    picture under an unchanged URL is the failure mode this codebase has already paid for once.
    """
    settings: Settings = request.app.state.settings
    deck = deck_service.get_deck(session, project_id)
    if deck is None:
        raise AppError(
            "deck_missing",
            "这个项目还没有演示稿",
            hint="先在「演示稿」里生成一份，再选字体。",
            status=404,
        )
    deck_service.set_fonts(
        session,
        settings,
        deck,
        title_font=payload.title_font,
        body_font=payload.body_font,
    )
    session.commit()
    pair = deck_service.view(session, project_id)
    assert pair is not None
    return _out(pair[0], pair[1], project_id)


@router.get("/projects/{project_id}/deck/fonts", response_model=FontListOut)
def list_deck_fonts(
    project_id: str, session: Session = Depends(_session_dependency)
) -> FontListOut:
    """Families this deck can be set in, plus the pair it is set in now.

    Chinese-capable only. The template tab's ``/api/fonts`` is the whole machine's list,
    because a poster's decorative face may legitimately be Latin-only — but every character of
    a slide is Chinese, and a face without the glyphs draws a page of empty boxes.
    """
    deck = deck_service.get_deck(session, project_id)
    title_font, body_font = (
        deck_service.deck_fonts(deck) if deck is not None else ("", "")
    )
    return FontListOut(
        families=fonts.chinese_families(),
        title_font=title_font,
        body_font=body_font,
    )


@router.post("/versions/{version_id}/deck/generate", response_model=DeckOut, status_code=201)
def generate_deck(
    version_id: str,
    payload: GenerateIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> DeckOut:
    """Plan the pages, falling back to the transcript cutter when the model cannot.

    Two paths, one endpoint, because the decision is not the client's to make: whether a model
    can be reached is a property of this machine's configuration, and a UI that has to choose
    would have to know. ``mode="transcript"`` is the explicit way to ask for the cheap one.

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

    source = "transcript"
    warnings: list[str] = []
    pages: list[PagePlan] = []
    project = project_service.get_project(session, version.project_id)
    if payload.mode == "plan":
        llm = build_llm(settings, session)
        if llm is None:
            warnings.append("没有配置 DASHSCOPE_API_KEY，按字幕停顿分页")
        else:
            with build_client() as client:
                outline = deck_outline.outline_pages(
                    session,
                    llm=llm,
                    client=client,
                    sentences=sentences,
                    chapters=chapters,
                    total_ms=version.duration_ms,
                    title_hint=project.name,
                    project_id=version.project_id,
                )
            pages = outline.pages
            warnings.extend(outline.warnings)
            if outline.source == "model":
                source = "model"
            else:
                warnings.append("模型没有给出可用的大纲，按字幕停顿分页")

    if source != "model":
        pages, fallback_warnings = settle_pages(
            plan_pages(sentences),
            sentences=sentences,
            total_ms=version.duration_ms,
            chapters=chapters,
        )
        warnings.extend(fallback_warnings)

    deck, slides = deck_service.generate_from_pages(
        session,
        settings,
        version.project_id,
        pages=pages,
        replace=payload.replace,
        source=source,
        warnings=warnings,
    )
    session.commit()
    return _out(deck, slides, version.project_id, warnings=warnings)


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
def delete_slide(
    slide_id: str, request: Request, session: Session = Depends(_session_dependency)
) -> None:
    """Remove one page and its PNG.

    It needs ``settings`` because the picture is a file, not a row — the earlier signature
    made that easy to forget, and forgetting it is invisible until someone counts the folder.
    """
    settings: Settings = request.app.state.settings
    deck_service.delete_slide(session, settings, slide_id)
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
    deck = deck_service.get_deck_by_id(session, deck_id)
    if deck is None:
        raise AppError("deck_missing", "演示稿不存在", status=404)
    path = deck_service.render_slide(
        settings,
        project_id,
        deck_id,
        row,
        width=width or 1440,
        height=height or 810,
        typefaces=deck_service.deck_fonts(deck),
    )
    return FileResponse(path, media_type="image/png")
