"""Outline extraction and glossary persistence.

LLM behaviour is mocked; what is under test is our own contract: the right
payload is sent, junk is filtered, nothing is written before review, and the
glossary/sync bookkeeping respects Bailian's quota and cooldown.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.core.hotwords import HotwordCandidate
from animatter.providers.http import build_client
from animatter.providers.llm.bailian import BailianLlm, parse_json_object
from animatter.services import hotwords as hotword_service
from animatter.services import outline as outline_service

# --- LLM client ---------------------------------------------------------


def make_llm(reply_content: str = "{}") -> BailianLlm:
    return BailianLlm(
        base_url="https://example.test/compatible-mode/v1", api_key="sk-test", model="qwen-plus"
    )


def call_llm(llm: BailianLlm, captured: dict, system: str = "s", user: str = "u"):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen-plus",
                "choices": [{"message": {"content": captured.get("reply", "{}")}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    # build_client disables trust_env; a bare Client() picks up this host's
    # socks ALL_PROXY and fails to parse it.
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        return llm.chat_json(system=system, user=user, client=client)


def test_llm_requests_json_mode() -> None:
    llm = make_llm()
    captured: dict[str, object] = {"reply": '{"terms": []}'}
    _payload, reply = call_llm(llm, captured)
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.1
    assert body["messages"][0]["role"] == "system"
    assert reply.prompt_tokens == 100


def test_llm_uses_compatible_endpoint() -> None:
    # base_url is the compatible root; the client appends /chat/completions.
    llm = make_llm()
    captured: dict[str, object] = {"reply": '{"terms": []}'}
    call_llm(llm, captured)
    assert str(captured["url"]).endswith("/chat/completions")


def test_llm_error_raises_app_error() -> None:
    llm = BailianLlm(base_url="https://example.test/compatible-mode/v1", api_key="k", model="qwen-plus")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": "Throttling.RateQuota", "message": "slow"})

    from animatter.errors import AppError

    # build_client disables trust_env; a bare Client() picks up this host's
    # socks ALL_PROXY and fails to parse it.
    with (
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client,
        pytest.raises(AppError, match="模型调用失败"),
    ):
        llm.chat(system="s", user="u", client=client)


# --- JSON parsing tolerance --------------------------------------------


def test_parse_plain_json() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_fenced_json() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_prose_prefix() -> None:
    assert parse_json_object('好的，结果如下：\n{"a": 1}') == {"a": 1}


def test_parse_invalid_returns_none() -> None:
    assert parse_json_object("这不是 JSON") is None


def test_parse_non_object_returns_none() -> None:
    assert parse_json_object("[1, 2]") is None


# --- extraction ---------------------------------------------------------


class FakeClient:
    pass


def run_extract(monkeypatch, payload: dict[str, object], outline: str = "大纲", meta: str = ""):  # type: ignore[no-untyped-def]
    llm = BailianLlm(base_url="https://example.test/compatible-mode/v1", api_key="k", model="qwen-plus")

    def fake_chat_json(**_kwargs):  # type: ignore[no-untyped-def]
        from animatter.providers.llm.bailian import LlmReply

        return payload, LlmReply(text="", prompt_tokens=10, completion_tokens=5, model="qwen-plus")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return outline_service.extract(outline_text=outline, llm=llm, client=build_client(), meta=meta)


def test_extract_keeps_valid_terms(monkeypatch) -> None:
    payload = {
        "terms": [
            {"text": "张三", "weight": 4, "kind": "person", "evidence": "嘉宾张三"},
            {"text": "百炼", "weight": 3, "kind": "product", "evidence": ""},
        ]
    }
    result = run_extract(monkeypatch, payload)
    assert {c.text for c in result.candidates} == {"张三", "百炼"}
    assert result.prompt_tokens == 10


def test_extract_filters_junk(monkeypatch) -> None:
    payload = {
        "terms": [
            {"text": "张三", "weight": 4},
            {"text": "我们", "weight": 3},
            {"text": "这是一个很长的句子，带标点"},
            {"text": "2024"},
        ]
    }
    result = run_extract(monkeypatch, payload)
    assert [c.text for c in result.candidates] == ["张三"]
    assert len(result.report.rejected) == 3


def test_extract_accepts_alternate_key(monkeypatch) -> None:
    result = run_extract(monkeypatch, {"hotwords": [{"text": "李四", "weight": 3}]})
    assert [c.text for c in result.candidates] == ["李四"]


def test_extract_empty_terms_warns(monkeypatch) -> None:
    result = run_extract(monkeypatch, {"terms": []})
    assert result.candidates == []
    assert any("没有返回" in w for w in result.warnings)


def test_extract_truncates_long_outline(monkeypatch) -> None:
    result = run_extract(monkeypatch, {"terms": []}, outline="字" * 20000)
    assert result.truncated is True


def test_extract_empty_outline_raises() -> None:
    from animatter.errors import AppError

    llm = BailianLlm(base_url="https://example.test/compatible-mode/v1", api_key="k", model="qwen-plus")
    with pytest.raises(AppError, match="大纲内容为空"):
        outline_service.extract(outline_text="   ", llm=llm, client=build_client())


def test_extract_bad_weight_falls_back_to_default(monkeypatch) -> None:
    result = run_extract(monkeypatch, {"terms": [{"text": "张三", "weight": "很高"}]})
    assert result.candidates[0].weight == 3


def test_extract_marks_origin(monkeypatch) -> None:
    result = run_extract(monkeypatch, {"terms": [{"text": "张三", "weight": 3}]})
    assert result.candidates[0].origin == "outline"


# --- database behaviour -------------------------------------------------


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def test_global_add_and_list(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    rows = hotword_service.list_global(session)
    assert [r.text for r in rows] == ["张三"]
    assert rows[0].weight == 4


def test_global_add_is_idempotent(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=2)])
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    rows = hotword_service.list_global(session)
    assert len(rows) == 1
    assert rows[0].weight == 4


def test_global_remove(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    hotword_service.remove_global(session, "张三")
    assert hotword_service.list_global(session) == []


def test_remove_missing_raises(session: Session) -> None:
    from animatter.errors import AppError

    with pytest.raises(AppError, match="没有"):
        hotword_service.remove_global(session, "不存在")


def test_hit_count_recorded(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    hotword_service.record_hit(session, "张三", delta=2)
    assert hotword_service.list_global(session)[0].hit_count == 2


def test_adding_marks_sync_dirty(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    state = hotword_service.sync_state(session)
    assert state.dirty is True
    assert state.content_hash != ""


def test_mark_synced_records_cooldown(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("张三", weight=4)])
    hotword_service.mark_synced(session, project_id=None, vocabulary_id="vocab-1", target_model="m")
    state = hotword_service.sync_state(session)
    assert state.dirty is False
    assert state.vocabulary_id == "vocab-1"
    assert state.next_allowed_at is not None
    assert state.next_allowed_at > state.last_synced_at  # type: ignore[operator]


def test_episode_replace(session: Session) -> None:
    hotword_service.replace_episode(
        session,
        project_id="prj_x",
        media_id="med_x",
        candidates=[HotwordCandidate("张三", weight=4)],
    )
    rows = hotword_service.list_episode(session, "prj_x", "med_x")
    assert [r.text for r in rows] == ["张三"]


def test_episode_replace_clears_old(session: Session) -> None:
    hotword_service.replace_episode(
        session, project_id="p", media_id="m", candidates=[HotwordCandidate("旧词", weight=3)]
    )
    hotword_service.replace_episode(
        session, project_id="p", media_id="m", candidates=[HotwordCandidate("新词", weight=3)]
    )
    assert [r.text for r in hotword_service.list_episode(session, "p", "m")] == ["新词"]


def test_effective_vocabulary_merges_global_and_episode(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("全局词", weight=4)])
    hotword_service.replace_episode(
        session, project_id="p", media_id="m", candidates=[HotwordCandidate("当期词", weight=3)]
    )
    vocabulary, trimmed = hotword_service.effective_vocabulary(session, project_id="p", media_id="m")
    assert vocabulary == {"全局词": 4, "当期词": 3}
    assert trimmed == 0


def test_effective_vocabulary_keeps_higher_weight(session: Session) -> None:
    hotword_service.add_global(session, [HotwordCandidate("同名词", weight=2)])
    hotword_service.replace_episode(
        session, project_id="p", media_id="m", candidates=[HotwordCandidate("同名词", weight=5)]
    )
    vocabulary, _ = hotword_service.effective_vocabulary(session, project_id="p", media_id="m")
    assert vocabulary["同名词"] == 5
