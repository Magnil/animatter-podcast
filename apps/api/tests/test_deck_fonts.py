"""Deck typefaces: title and body set separately (§19.17).

Three things are being pinned, and each of them fails *silently* rather than loudly if it
regresses:

* **The two roles are independent.** Moving only the title's family, and only the body's,
  has to change the page's pixels. Applying one family to both is what the deck did before
  this round, and it looks plausible on screen — the page renders, it is simply not what was
  asked for.
* **The picture changes when the font does.** The PNG's path carries the pair, and
  ``set_fonts`` empties the folder; without either, the editor and the film keep serving the
  old face under an unchanged URL (§19.11 trap 24).
* **An unusable family is refused at the boundary.** A Latin-only face on a Chinese page draws
  empty boxes, and a missing one 503s on every later render with nothing the user can act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.errors import AppError
from animatter.media import fonts, slide_render
from animatter.services import decks as deck_service

#: Past ``TARGET_PAGE_MS`` (60 s), so one fixture sentence is one page.
SPAN = 70_000


def _pages(count: int) -> list[object]:
    from animatter.core.script import ScriptSentence

    return [
        ScriptSentence(
            ord=index,
            start_ms=index * (SPAN + 800),
            end_ms=index * (SPAN + 800) + SPAN,
            speaker=0,
            text=f"第{index}页的内容。",
        )
        for index in range(count)
    ]


@pytest.fixture()
def project(session: Session) -> str:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    return version.project_id


@pytest.fixture()
def app_settings(client: object) -> object:
    return client.app.state.settings  # type: ignore[attr-defined]


def _two_families() -> tuple[str, str]:
    """The first two distinct Chinese-capable families this machine has, sorted stably."""
    available = fonts.chinese_families()
    wanted = [name for name in ("SimHei", "KaiTi", "SimSun", "Microsoft YaHei") if name in available]
    if len(wanted) < 2:
        pytest.skip(f"需要两个中文字体才能测「标题和正文分开」，这台机器上只有 {available}")
    return wanted[0], wanted[1]


# --- the stored pair ----------------------------------------------------


def test_a_new_deck_uses_the_renderer_default_for_both_roles(
    session: Session, app_settings: object, project: str
) -> None:
    """Unset is stored as ``""`` and *reported* resolved.

    The panel binds its pickers to the reported pair, so an empty string there would render as
    a blank option instead of the face the preview is actually in.
    """
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    assert deck.title_font == ""
    assert deck.body_font == ""
    assert deck_service.deck_fonts(deck) == (
        slide_render.FONT_FAMILY,
        slide_render.FONT_FAMILY,
    )


def test_the_two_roles_are_set_independently(
    session: Session, app_settings: object, project: str
) -> None:
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, body_family = _two_families()

    deck_service.set_fonts(session, app_settings, deck, title_font=title_family)
    assert deck_service.deck_fonts(deck) == (title_family, slide_render.FONT_FAMILY)

    deck_service.set_fonts(session, app_settings, deck, body_font=body_family)
    assert deck_service.deck_fonts(deck) == (title_family, body_family)


def test_an_absent_argument_leaves_the_other_role_alone(
    session: Session, app_settings: object, project: str
) -> None:
    """``None`` is "don't touch"; ``""`` is "back to the default". They must not be the same."""
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, body_family = _two_families()
    deck_service.set_fonts(
        session, app_settings, deck, title_font=title_family, body_font=body_family
    )

    deck_service.set_fonts(session, app_settings, deck, body_font="")
    assert deck_service.deck_fonts(deck) == (title_family, slide_render.FONT_FAMILY)


def test_a_font_that_cannot_draw_chinese_is_refused(
    session: Session, app_settings: object, project: str
) -> None:
    """Refused at the write, not at the next render.

    A Latin-only family on a Chinese page draws a page of blank boxes. Accepting it here would
    store a deck that looks broken on every preview afterwards, with the choice three screens
    away and no error naming it.
    """
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    latin = [
        name
        for name in fonts.families()
        if not fonts.draws_chinese(name) and name.isascii()
    ]
    if not latin:
        pytest.skip("这台机器上一个纯西文字体都没有")
    with pytest.raises(AppError) as caught:
        deck_service.set_fonts(session, app_settings, deck, title_font=latin[0])
    assert caught.value.code == "slide_font_unusable"
    assert latin[0] in caught.value.message
    assert caught.value.hint, "an error with no way forward is a bug (§8)"


def test_an_unknown_family_is_refused_too(
    session: Session, app_settings: object, project: str
) -> None:
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    with pytest.raises(AppError) as caught:
        deck_service.set_fonts(session, app_settings, deck, body_font="不存在的字体 ABCXYZ")
    assert caught.value.code == "slide_font_unusable"


def test_whitespace_clears_back_to_the_default(
    session: Session, app_settings: object, project: str
) -> None:
    """The picker cannot send a blank, but a hand-made request can — and it means "default"."""
    deck, _slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, _body = _two_families()
    deck_service.set_fonts(session, app_settings, deck, title_font=title_family)
    deck_service.set_fonts(session, app_settings, deck, title_font="   ")
    assert deck.title_font == ""


# --- the picture --------------------------------------------------------


def test_the_title_family_changes_the_pixels_by_itself(
    session: Session, app_settings: object, project: str
) -> None:
    """Changing only the title's family must change the page.

    Checked in pixels rather than in the row: a renderer that quietly used one family for both
    roles would still pass every assertion about the stored strings.
    """
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, body_family = _two_families()
    row = slides[0].row

    one = _bytes(
        deck_service.render_slide(
            app_settings, project, deck.id, row,
            typefaces=(body_family, body_family), width=720, height=405,
        )
    )
    two = _bytes(
        deck_service.render_slide(
            app_settings, project, deck.id, row,
            typefaces=(title_family, body_family), width=720, height=405,
        )
    )
    assert one != two


def test_the_body_family_changes_the_pixels_by_itself(
    session: Session, app_settings: object, project: str
) -> None:
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, body_family = _two_families()
    row = slides[0].row

    one = _bytes(
        deck_service.render_slide(
            app_settings, project, deck.id, row,
            typefaces=(title_family, title_family), width=720, height=405,
        )
    )
    two = _bytes(
        deck_service.render_slide(
            app_settings, project, deck.id, row,
            typefaces=(title_family, body_family), width=720, height=405,
        )
    )
    assert one != two


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


def test_the_png_path_carries_the_typefaces(
    session: Session, app_settings: object, project: str
) -> None:
    """Same page, same size, two fonts → two files, so neither can be served for the other."""
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    title_family, body_family = _two_families()
    a = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row,
        typefaces=(title_family, title_family), width=320, height=180,
    )
    b = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row,
        typefaces=(title_family, body_family), width=320, height=180,
    )
    assert a != b
    assert a.name.endswith(".png") and b.name.endswith(".png")


def test_changing_the_fonts_deletes_every_cached_picture(
    session: Session, app_settings: object, project: str
) -> None:
    """A folder left behind is a folder the next render can serve a stale face from.

    ``set_fonts`` purges it, and that is the only thing that collects the *previous* pair's
    files: their names are no longer reachable, so nothing else will ever name them again.
    """
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(2), total_ms=2 * SPAN
    )
    for view in slides:
        deck_service.render_slide(
            app_settings, project, deck.id, view.row, typefaces=("", ""), width=320, height=180
        )
    directory = deck_service.slides_dir(app_settings, project, deck.id)
    assert list(directory.glob("*.png")), "the fixture must have drawn something"

    title_family, _body = _two_families()
    deck_service.set_fonts(session, app_settings, deck, title_font=title_family)
    assert not list(directory.glob("*.png"))


def test_setting_the_same_font_again_keeps_the_pictures(
    session: Session, app_settings: object, project: str
) -> None:
    """A no-op must not cost a re-render of every page.

    The picker reports the *resolved* family, so choosing the default from the list sends the
    same family the deck is already effectively set in. Purging then would throw away 30 PNGs
    (and re-draw them) for a change of zero pixels.
    """
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, typefaces=("", ""), width=320, height=180
    )
    directory = deck_service.slides_dir(app_settings, project, deck.id)
    before = sorted(path.name for path in directory.glob("*.png"))

    deck_service.set_fonts(session, app_settings, deck, title_font=slide_render.FONT_FAMILY)
    assert sorted(path.name for path in directory.glob("*.png")) == before


def test_an_unset_deck_and_an_explicitly_default_deck_share_one_picture(
    session: Session, app_settings: object, project: str
) -> None:
    """``""`` and the default family resolve to the same key — one file, not two copies.

    Two copies of identical pixels is not a correctness bug, but it is the kind of thing that
    makes a folder of 30 pages read as 60 and a "why is my disk full" question unanswerable.
    """
    deck, slides = deck_service.generate_from_transcript(
        session, app_settings, project, sentences=_pages(1), total_ms=SPAN
    )
    unset = deck_service.render_slide(
        app_settings, project, deck.id, slides[0].row, typefaces=("", ""), width=320, height=180
    )
    explicit = deck_service.render_slide(
        app_settings,
        project,
        deck.id,
        slides[0].row,
        typefaces=(slide_render.FONT_FAMILY, slide_render.FONT_FAMILY),
        width=320,
        height=180,
    )
    assert unset == explicit


# --- the API ------------------------------------------------------------


def test_the_deck_endpoint_reports_the_resolved_pair(
    client: TestClient, session: Session
) -> None:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    response = client.post(
        f"/api/versions/{version.id}/deck/generate",
        json={"replace": True, "mode": "transcript"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["title_font"] == slide_render.FONT_FAMILY
    assert payload["body_font"] == slide_render.FONT_FAMILY


def test_patching_the_fonts_returns_the_new_pair(
    client: TestClient, session: Session
) -> None:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    project_id = version.project_id
    client.post(
        f"/api/versions/{version.id}/deck/generate",
        json={"replace": True, "mode": "transcript"},
    )
    title_family, body_family = _two_families()

    response = client.patch(
        f"/api/projects/{project_id}/deck",
        json={"title_font": title_family, "body_font": body_family},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["title_font"] == title_family
    assert payload["body_font"] == body_family

    fetched = client.get(f"/api/projects/{project_id}/deck").json()
    assert (fetched["title_font"], fetched["body_font"]) == (title_family, body_family)


def test_patching_a_missing_deck_is_a_404_with_a_hint(
    client: TestClient, session: Session
) -> None:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    response = client.patch(
        f"/api/projects/{version.project_id}/deck", json={"title_font": "SimHei"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["hint"]


def test_an_unusable_font_is_a_400_from_the_route(
    client: TestClient, session: Session
) -> None:
    version = seed_version(session, text="素材", step=100)
    session.commit()
    project_id = version.project_id
    client.post(
        f"/api/versions/{version.id}/deck/generate",
        json={"replace": True, "mode": "transcript"},
    )
    response = client.patch(
        f"/api/projects/{project_id}/deck", json={"title_font": "不存在的字体 ABCXYZ"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "slide_font_unusable"


def test_the_font_list_offers_only_chinese_capable_families(
    client: TestClient, session: Session
) -> None:
    """The picker must not offer a face that draws empty boxes.

    ``/api/fonts`` (the template tab's list) deliberately stays wider: a poster's episode
    number is digits, and the design's Latin faces are right for it. This endpoint is the one
    that knows every character on a slide is Chinese.
    """
    version = seed_version(session, text="素材", step=100)
    session.commit()
    response = client.get(f"/api/projects/{version.project_id}/deck/fonts")
    assert response.status_code == 200, response.text
    families = response.json()["families"]
    assert families, "a machine with no Chinese font cannot render a deck at all"
    assert all(fonts.draws_chinese(name) for name in families)

    wider = client.get("/api/fonts").json()["families"]
    assert len(families) <= len(wider)


def test_the_image_route_draws_with_the_deck_s_fonts(
    client: TestClient, session: Session
) -> None:
    """The URL does not change when the font does, so the route has to read the deck itself."""
    version = seed_version(session, text="素材", step=100)
    session.commit()
    project_id = version.project_id
    deck = client.post(
        f"/api/versions/{version.id}/deck/generate",
        json={"replace": True, "mode": "transcript"},
    ).json()
    title_family, body_family = _two_families()

    first = client.get(deck["slides"][0]["image_url"], params={"width": 480, "height": 270})
    assert first.status_code == 200, first.text

    client.patch(
        f"/api/projects/{project_id}/deck",
        json={"title_font": title_family, "body_font": body_family},
    )
    second = client.get(deck["slides"][0]["image_url"], params={"width": 480, "height": 270})
    assert second.status_code == 200, second.text
    assert second.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert first.content != second.content, "the same URL served the old face"
