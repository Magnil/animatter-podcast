"""Deck and slide API shapes (§19.6).

The tests that matter here are the round-trips: generate → edit → move → fetch the image.
Each one exercises a mistake that produces a *plausible* wrong answer rather than an error:
a page whose PNG is stale after an edit, a move that silently reorders nothing because the
unique index swallowed the write, and an ``image_url`` that looks valid and 404s.

The seed is deliberately built so a page boundary is actually possible: a page is ~a minute
of speech now, so the fixture has to be three sentences of 70 s each with a real pause
between them. A 4-second sentence is a page fragment and gets merged into its neighbour.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.models import Token

#: ``seed_version``'s ``sizes`` is **tokens per sentence**, not characters — a wrong split
#: silently yields one-token sentences and every page test degenerates to a single page.
TEXT = "第一句要够长。第二句也要够长。第三句同样够长。"
SIZES = [7, 8, 8]
#: One fixture sentence = one page. Past ``TARGET_PAGE_MS`` (60 s) on its own.
PER_SENTENCE = 70_000
#: A pause between them, past ``PAGE_GAP_MS`` (700 ms).
GAP = 800


@pytest.fixture()
def version(session: Session) -> object:
    """Three sentences, each a page, with real pauses between them."""
    from animatter.models import Sentence

    assert sum(SIZES) == len(TEXT), "sizes must partition the text"
    row = seed_version(session, text=TEXT, sizes=SIZES, step=1000)
    session.flush()

    tokens = session.scalars(
        select(Token).where(Token.version_id == row.id).order_by(Token.idx)
    ).all()
    sentences = session.scalars(
        select(Sentence).where(Sentence.version_id == row.id).order_by(Sentence.ord)
    ).all()
    assert len(sentences) >= 3, "the seed must have at least three sentences"

    # Lay the three sentences out end to end with a real pause between them, each long
    # enough to be its own page.
    cursor = 0
    for index, sentence in enumerate(sentences):
        if index >= 3:
            break
        count = sentence.token_end - sentence.token_start
        per = PER_SENTENCE // max(count, 1)
        for offset, token in enumerate(tokens[sentence.token_start : sentence.token_end]):
            token.start_ms = cursor + offset * per
            token.end_ms = cursor + (offset + 1) * per
        cursor += count * per + GAP
    row.duration_ms = cursor
    session.commit()
    return row


def generate(client: TestClient, version_id: str, **body: object) -> dict[str, object]:
    # ``mode="transcript"`` explicitly: this file pins the fallback path's API shape, and
    # leaving it implicit would make every assertion here depend on whether the machine
    # happens to have a model key (round 100 made ``plan`` the default). The model path has
    # its own file, with the call stubbed.
    response = client.post(
        f"/api/versions/{version_id}/deck/generate",
        json={"replace": True, "mode": "transcript", **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- the collection -----------------------------------------------------


def test_a_project_without_a_deck_gets_null(client: TestClient, version: object) -> None:
    """``null`` rather than 404: "no deck yet" is the normal starting state."""
    project_id = version.project_id  # type: ignore[attr-defined]
    response = client.get(f"/api/projects/{project_id}/deck")
    assert response.status_code == 200
    assert response.json() is None


def test_generating_returns_the_pages(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    payload = generate(client, version_id)
    slides = payload["slides"]
    assert isinstance(slides, list)
    assert len(slides) >= 1
    first = slides[0]
    assert first["title"]
    assert first["body"]
    assert first["ord"] == 0
    assert first["layout"] == "title_body"
    assert first["source_ord"] == 0


def test_generating_twice_replaces_the_pages(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    first = generate(client, version_id)
    second = generate(client, version_id)
    assert first["id"] == second["id"], "one deck per project"
    assert len(second["slides"]) == len(first["slides"])  # type: ignore[arg-type]


def test_pages_carry_absolute_milliseconds(client: TestClient, version: object) -> None:
    """A video block snaps to these, so they must be source-timeline values."""
    version_id = version.id  # type: ignore[attr-defined]
    payload = generate(client, version_id)
    slides = payload["slides"]
    assert isinstance(slides, list)
    for index, slide in enumerate(slides):
        assert slide["start_ms"] >= 0
        if index:
            assert slide["start_ms"] >= slides[index - 1]["start_ms"]


# --- editing ------------------------------------------------------------


def test_editing_a_page_changes_what_is_returned(
    client: TestClient, version: object
) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    slide_id = generate(client, version_id)["slides"][0]["id"]  # type: ignore[index]
    response = client.patch(
        f"/api/slides/{slide_id}", json={"title": "人工改的标题", "body": "人工改的正文"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "人工改的标题"
    assert response.json()["body"] == "人工改的正文"


def test_an_unknown_layout_is_rejected(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    row_id = generate(client, version_id)["slides"][0]["id"]  # type: ignore[index]
    response = client.patch(f"/api/slides/{row_id}", json={"layout": "gigantic"})
    assert response.status_code == 422 or response.status_code == 400
    assert "版式" in response.text


def test_editing_an_unknown_slide_is_a_404(client: TestClient) -> None:
    response = client.patch("/api/slides/sld_nope", json={"title": "x"})
    assert response.status_code == 404


# --- images -------------------------------------------------------------


def test_the_image_url_the_response_returns_actually_works(
    client: TestClient, version: object
) -> None:
    """The URL must be fetched as returned, not assembled by the test.

    §19.11.11: a client-built URL can look valid and 404, which is exactly what happened to
    the template asset route.
    """
    version_id = version.id  # type: ignore[attr-defined]
    project_id = version.project_id  # type: ignore[attr-defined]
    generate(client, version_id)

    fresh = client.get(f"/api/projects/{project_id}/deck").json()
    url = fresh["slides"][0]["image_url"]
    assert url, "every slide must hand out its image URL, drawn or not"

    image = client.get(url)
    assert image.status_code == 200, image.text
    assert image.headers["content-type"] == "image/png"
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_image_can_be_requested_at_another_size(
    client: TestClient, version: object
) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    project_id = version.project_id  # type: ignore[attr-defined]
    deck = generate(client, version_id)
    slide_id = deck["slides"][0]["id"]
    url = f"/api/projects/{project_id}/decks/{deck['id']}/slides/{slide_id}/image"
    response = client.get(url, params={"width": 480, "height": 270})
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size == (480, 270)


def test_an_image_for_a_slide_of_another_deck_is_a_404(
    client: TestClient, version: object
) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    project_id = version.project_id  # type: ignore[attr-defined]
    deck = generate(client, version_id)
    slide_id = deck["slides"][0]["id"]
    response = client.get(
        f"/api/projects/{project_id}/decks/deck_other/slides/{slide_id}/image"
    )
    assert response.status_code == 404


# --- ordering -----------------------------------------------------------


def test_moving_a_page_returns_the_new_order(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    deck = generate(client, version_id)
    slides = deck["slides"]
    assert len(slides) >= 3, f"the seed must produce three pages, got {len(slides)}"
    first_id = slides[0]["id"]
    response = client.post(f"/api/slides/{first_id}/move", json={"to_ord": len(slides) - 1})
    assert response.status_code == 200
    order = [item["id"] for item in response.json()]
    assert order[-1] == first_id
    assert [item["ord"] for item in response.json()] == list(range(len(slides)))


def test_deleting_a_page_is_idempotent(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    slide_id = generate(client, version_id)["slides"][0]["id"]  # type: ignore[index]
    assert client.delete(f"/api/slides/{slide_id}").status_code == 204
    assert client.delete(f"/api/slides/{slide_id}").status_code == 204


# --- chapters -----------------------------------------------------------


def test_chapter_openings_become_page_titles(client: TestClient, version: object) -> None:
    version_id = version.id  # type: ignore[attr-defined]
    payload = generate(
        client,
        version_id,
        chapters=[{"heading": "第一章 开场", "start_ms": 0}],
    )
    slides = payload["slides"]
    assert isinstance(slides, list)
    assert slides[0]["title"] == "第一章 开场"


def test_a_chapter_start_that_matches_no_sentence_still_works(
    client: TestClient, version: object
) -> None:
    """Hand-written chapters sit up to 6 s off the sentence they name."""
    version_id = version.id  # type: ignore[attr-defined]
    payload = generate(
        client,
        version_id,
        chapters=[{"heading": "偏了一点的章节", "start_ms": 7_500}],
    )
    assert isinstance(payload["slides"], list)


# --- the model path ------------------------------------------------------


class _NullClient:
    def __enter__(self) -> _NullClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def stub_outline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, reply: str
) -> list[str]:
    """Stub the model at the provider boundary and return the prompts it was given.

    Stubbed at ``BailianLlm.chat`` rather than at ``deck_outline``: the prompt, the line
    numbering and the block splitting are all things this file is asserting, and a stub one
    level up would let them be wrong while the tests stayed green.

    The key goes on the **app's settings instance**, not on the ``Settings`` class: conftest
    leaves it ``None`` so that no test can make a real paid call by accident, and a pydantic
    instance holds its own value, so a class-level attribute would not be read.
    """
    from animatter.providers.llm.bailian import BailianLlm, LlmReply

    seen: list[str] = []

    def fake_chat(
        self: object,
        *,
        system: str,
        user: str,
        client: object,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        json_mode: bool = True,
    ) -> LlmReply:
        seen.append(user)
        return LlmReply(text=reply, prompt_tokens=10, completion_tokens=10, model="qwen-plus")

    monkeypatch.setattr(BailianLlm, "chat", fake_chat)
    monkeypatch.setattr("animatter.routers.decks.build_client", lambda *a, **k: _NullClient())
    client.app.state.settings.dashscope_api_key = "test-key"  # type: ignore[attr-defined]
    return seen


def test_the_plan_mode_asks_the_model_and_says_so(
    client: TestClient, version: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of round 100: pages come from an outline, not from the transcript."""
    prompt_seen = stub_outline(
        client,
        monkeypatch,
        '{"pages": [{"line": 0, "title": "开场", "points": ["为什么做这期", "这期聊什么"]}]}',
    )
    version_id = version.id  # type: ignore[attr-defined]
    response = client.post(
        f"/api/versions/{version_id}/deck/generate", json={"replace": True, "mode": "plan"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["warnings"] == []
    assert prompt_seen, "the model must actually have been called"
    assert "第 0 行" not in prompt_seen[0], "the prompt numbers lines, it does not restate them"
    assert "[0]" in prompt_seen[0], "line numbers are what the reply cites back"

    slides = payload["slides"]
    assert slides[0]["title"] == "开场"
    assert "・为什么做这期" in slides[0]["body"]
    assert "・这期聊什么" in slides[0]["body"]


def test_a_reply_with_no_usable_pages_falls_back_and_says_so(
    client: TestClient, version: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fallback deck must never arrive looking like a planned one."""
    stub_outline(client, monkeypatch, '{"pages": []}')
    version_id = version.id  # type: ignore[attr-defined]
    response = client.post(
        f"/api/versions/{version_id}/deck/generate", json={"replace": True, "mode": "plan"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["source"] == "transcript"
    assert any("没有给出可用的大纲" in warning for warning in payload["warnings"])
    assert payload["slides"], "the fallback must still produce a deck"


def test_a_failed_model_call_still_produces_a_deck(
    client: TestClient, version: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One timeout must not cost the user the feature."""
    from animatter.errors import AppError
    from animatter.providers.llm.bailian import BailianLlm

    def boom(*args: object, **kwargs: object) -> object:
        raise AppError("llm_call_failed", "模型调用失败（HTTP 502）", status=502)

    monkeypatch.setattr(BailianLlm, "chat", boom)
    monkeypatch.setattr("animatter.routers.decks.build_client", lambda *a, **k: _NullClient())
    client.app.state.settings.dashscope_api_key = "test-key"  # type: ignore[attr-defined]

    version_id = version.id  # type: ignore[attr-defined]
    response = client.post(
        f"/api/versions/{version_id}/deck/generate", json={"replace": True, "mode": "plan"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["source"] == "transcript"
    assert payload["slides"]
    assert any("规划失败" in warning for warning in payload["warnings"])


def test_without_a_key_the_deck_is_generated_and_labelled(
    client: TestClient, version: object
) -> None:
    """No key is a normal state, not an error: the free path runs and the panel is told."""
    version_id = version.id  # type: ignore[attr-defined]
    response = client.post(
        f"/api/versions/{version_id}/deck/generate", json={"replace": True, "mode": "plan"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["source"] == "transcript"
    assert any("DASHSCOPE_API_KEY" in warning for warning in payload["warnings"])


def test_the_provenance_survives_a_re_read(client: TestClient, version: object) -> None:
    """The panel re-reads the deck on mount, so ``source`` cannot live only in the response."""
    version_id = version.id  # type: ignore[attr-defined]
    project_id = version.project_id  # type: ignore[attr-defined]
    generated = generate(client, version_id)
    assert generated["source"] == "transcript"
    fetched = client.get(f"/api/projects/{project_id}/deck").json()
    assert fetched["source"] == "transcript"
    assert fetched["warnings"] == generated["warnings"]


# --- failed generation --------------------------------------------------


def test_an_unknown_version_is_a_404(client: TestClient) -> None:
    response = client.post("/api/versions/ver_nope/deck/generate", json={})
    assert response.status_code == 404
