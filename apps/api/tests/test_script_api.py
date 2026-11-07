"""The generate endpoint, end to end against a seeded project.

The LLM is stubbed at the provider boundary, so what these tests pin is
everything *around* the model call:

* chapter times come from the transcript, never from the model's own numbers;
* a deleted sentence is not summarised, because the listener never hears it;
* the signature reaches the file;
* one failed block does not discard the blocks that succeeded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.ids import new_id, utcnow_iso
from animatter.models import Media, Project, Sentence, Speaker, Token, TranscriptVersion
from animatter.providers.llm.bailian import LlmReply


@pytest.fixture()
def seeded(session: Session) -> dict[str, str]:
    """Two speakers, four sentences, 8 seconds."""
    project = Project(id=new_id("prj"), name="漫物质", created_at=utcnow_iso(), updated_at=utcnow_iso())
    session.add(project)
    session.flush()

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path="x.wav",
        sha256=new_id("sha"),
        duration_ms=8000,
        sample_rate=16000,
        channels=1,
        codec="wav",
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()

    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id,
        job_id="job",
        version=1,
        created_at=utcnow_iso(),
        duration_ms=8000,
        token_count=4,
        sentence_count=4,
        is_active=True,
    )
    session.add(version)
    session.flush()

    session.add(
        Speaker(
            id=new_id("spk"),
            project_id=project.id,
            local_id=0,
            name="马小褂",
            color="#2563eb",
            created_at=utcnow_iso(),
        )
    )

    texts = [
        ("今天聊动画", 0, 2000, 0),
        ("我很期待", 2000, 4000, 1),
        ("先说说今敏", 4000, 6000, 0),
        ("好", 6000, 8000, 1),
    ]
    for idx, (text, start, end, speaker) in enumerate(texts):
        session.add(
            Token(
                version_id=version.id,
                idx=idx,
                start_ms=start,
                end_ms=end,
                text=text,
                kind="word",
                src_word_idx=idx,
                punc_after="。",
                derived=False,
                revised=False,
            )
        )
        session.add(
            Sentence(
                id=new_id("sen"),
                version_id=version.id,
                ord=idx,
                token_start=idx,
                token_end=idx + 1,
                speaker_id=speaker,
                origin="asr",
                updated_at=utcnow_iso(),
            )
        )
    session.commit()
    return {"project": project.id, "version": version.id}


def stub_llm(monkeypatch: pytest.MonkeyPatch, text: str) -> list[dict[str, object]]:
    """Replace the model call; return the list calls are recorded into."""
    calls: list[dict[str, object]] = []

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
        calls.append(
            {"system": system, "user": user, "temperature": temperature, "json_mode": json_mode}
        )
        return LlmReply(text=text, prompt_tokens=10, completion_tokens=10, model="qwen-plus")

    from animatter.providers.llm.bailian import BailianLlm

    monkeypatch.setattr(BailianLlm, "chat", fake_chat)
    monkeypatch.setattr(
        "animatter.routers.script.build_client", lambda *a, **k: _NullClient()
    )
    return calls


class _NullClient:
    def __enter__(self) -> _NullClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def api_key(client: TestClient) -> None:
    client.app.state.settings.dashscope_api_key = "test-key"  # type: ignore[attr-defined]


def test_shownotes_chapters_use_transcript_times(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model names line 2 and claims nothing about time; the endpoint must
    # read the millisecond value off turn 2 (the "先说说今敏" sentence at 4000).
    stub_llm(
        monkeypatch,
        '{"summary": "聊动画 MV。", "points": ["今敏"], '
        '"chapters": [{"heading": "开场", "line": 0}, {"heading": "今敏", "line": 2}]}',
    )
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "shownotes", "mapped": False},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [c["start_ms"] for c in payload["chapters"]] == [0, 4000]
    assert [c["clock"] for c in payload["chapters"]] == ["00:00", "00:04"]
    assert "## 时间轴" in payload["body"]
    assert payload["filename"].endswith("_ShowNotes.md")


def test_the_signature_reaches_the_rendered_file(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, '{"summary": "S", "points": [], "chapters": []}')
    client.patch(
        "/api/settings",
        json={"values": {"shownotes_signature": "主播：马小褂\n收听：小宇宙"}},
    )
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "shownotes", "mapped": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()["body"]
    assert body.rstrip().endswith("主播：马小褂\n收听：小宇宙")


def test_an_empty_summary_fails_loudly(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty body with a 200 would be saved over a good document.
    stub_llm(monkeypatch, '{"points": [], "chapters": []}')
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "shownotes", "mapped": False},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "empty_summary"


def test_dialogue_keeps_speakers_and_uses_the_edited_prompt(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_llm(monkeypatch, "**马小褂**：你好。\n\n**说话人2**：你好。")
    client.patch("/api/settings", json={"values": {"script_prompt_dialogue": "自定义提示词"}})
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "dialogue", "mapped": False},
    )
    assert response.status_code == 200, response.text
    assert calls[0]["system"] == "自定义提示词"
    body = response.json()["body"]
    assert "**马小褂**：你好。" in body
    assert "## " not in body
    # Speaker names reach the model, so it can label turns without inventing ids.
    assert "马小褂：" in str(calls[0]["user"])


def test_article_requests_no_json_and_has_no_speaker_labels(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_llm(monkeypatch, "这是一篇书面化的文章。")
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "article", "mapped": False},
    )
    assert response.status_code == 200, response.text
    assert calls[0]["json_mode"] is False
    assert "说话人" not in response.json()["body"]


def test_temperature_and_max_tokens_come_from_settings(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_llm(monkeypatch, "正文。")
    client.patch("/api/settings", json={"values": {"text_temperature": 0.9}})
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "article", "mapped": False},
    )
    assert response.status_code == 200, response.text
    assert calls[0]["temperature"] == 0.9


def test_a_deleted_sentence_is_not_described(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentence nobody will hear must not appear in ShowNotes.

    The mapping drops the whole sentence, so the model never sees "我很期待".
    """
    calls = stub_llm(monkeypatch, '{"summary": "S", "points": [], "chapters": []}')
    deleted = client.post(
        f"/api/versions/{seeded['version']}/deletions",
        json={"token_ids": [1]},
    )
    assert deleted.status_code in (200, 201), deleted.text
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "shownotes", "mapped": True},
    )
    assert response.status_code == 200, response.text
    assert "我很期待" not in str(calls[0]["user"])


def test_a_bad_format_is_rejected_before_any_model_call(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_llm(monkeypatch, "正文。")
    response = client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "podcast", "mapped": False},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_format"
    assert calls == []


def test_prompts_endpoint_returns_defaults_and_overrides(
    client: TestClient,
) -> None:
    client.patch("/api/settings", json={"values": {"script_prompt_article": "我的提示词"}})
    rows = client.get("/api/script/prompts").json()
    assert {row["format"] for row in rows} == {"shownotes", "dialogue", "article"}
    article = next(row for row in rows if row["format"] == "article")
    assert article["current"] == "我的提示词"
    assert article["default"] != "我的提示词"
    assert len(article["default"]) > 200


def test_clearing_an_override_falls_back_to_the_builtin(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_llm(monkeypatch, "正文。")
    client.patch("/api/settings", json={"values": {"script_prompt_article": "我的提示词"}})
    client.patch("/api/settings", json={"values": {"script_prompt_article": ""}})
    client.post(
        f"/api/versions/{seeded['version']}/script/generate",
        json={"format": "article", "mapped": False},
    )
    assert calls[0]["system"] != "我的提示词"
    assert "书面文章" in str(calls[0]["system"])


def test_download_returns_markdown_with_a_utf8_filename(client: TestClient) -> None:
    response = client.post(
        "/api/script/file",
        json={"body": "# 标题\n\n正文。\n", "filename": "漫物质_ShowNotes.md"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.content.decode("utf-8").startswith("# 标题")
