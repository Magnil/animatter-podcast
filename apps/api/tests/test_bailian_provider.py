"""Request assembly and response parsing for the Bailian provider.

Parameter correctness is not cosmetic here: a wrong model for the upload policy,
or a dropped ``X-DashScope-OssResourceResolve`` header, produces a file that is
silently unreachable rather than an error. These tests pin the exact wire
format (AGENTS.md §2.1).
"""

from __future__ import annotations

import httpx
import pytest

from animatter.providers.asr.bailian import BailianAsrProvider, parse_result
from animatter.providers.asr.base import AsrRequest


def make_provider(model: str = "qwen-audio-3.1-asr-flash-filetrans") -> BailianAsrProvider:
    return BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model=model,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )


def body_for(provider: BailianAsrProvider, **overrides: object) -> dict:
    from dataclasses import replace

    request = replace(AsrRequest(file_url="oss://bucket/a.mp3", model=provider._model), **overrides)  # type: ignore[arg-type]
    return provider._build_body(request)


# --- capabilities -------------------------------------------------------


def test_qwen31_declares_full_capabilities() -> None:
    caps = make_provider().capabilities
    assert caps.instant_vocabulary is True
    assert caps.context is True
    assert caps.super_hotword_weight is True
    assert caps.max_hotwords == 2000


def test_paraformer_has_neither_instant_vocabulary_nor_context() -> None:
    caps = make_provider("paraformer-v2").capabilities
    assert caps.instant_vocabulary is False
    assert caps.context is False
    assert caps.precompiled_vocabulary is True
    assert caps.max_hotwords == 500


def test_unknown_model_is_conservative() -> None:
    caps = make_provider("some-future-model").capabilities
    assert caps.instant_vocabulary is False
    assert caps.context is False


# --- request body -------------------------------------------------------


def test_body_has_required_shape() -> None:
    body = body_for(make_provider())
    assert body["model"] == "qwen-audio-3.1-asr-flash-filetrans"
    assert body["input"]["file_urls"] == ["oss://bucket/a.mp3"]
    assert body["parameters"]["channel_id"] == [0]
    assert body["parameters"]["language_hints"] == ["zh", "en"]


def test_instant_vocabulary_is_sent_when_supported() -> None:
    body = body_for(make_provider(), vocabulary={"张三": 4})
    assert body["parameters"]["vocabulary"] == {"张三": 4}


def test_instant_vocabulary_is_dropped_for_paraformer() -> None:
    body = body_for(make_provider("paraformer-v2"), vocabulary={"张三": 4})
    assert "vocabulary" not in body["parameters"]


def test_precompiled_vocabulary_id_is_sent() -> None:
    body = body_for(make_provider(), vocabulary_id="vocab-abc")
    assert body["parameters"]["vocabulary_id"] == "vocab-abc"


def test_context_is_wrapped_as_user_message() -> None:
    body = body_for(make_provider(), context_text="本期嘉宾：某某")
    context = body["input"]["context"]
    assert context[0]["role"] == "user"
    assert context[0]["content"][0]["type"] == "input_text"
    assert context[0]["content"][0]["text"] == "本期嘉宾：某某"


def test_context_is_truncated_to_400_chars() -> None:
    body = body_for(make_provider(), context_text="字" * 900)
    assert len(body["input"]["context"][0]["content"][0]["text"]) == 400


def test_context_dropped_for_paraformer() -> None:
    body = body_for(make_provider("paraformer-v2"), context_text="上下文")
    assert "context" not in body["input"]


def test_diarization_sets_flag_and_speaker_count() -> None:
    body = body_for(make_provider(), diarization=True, speaker_count=3)
    assert body["parameters"]["diarization_enabled"] is True
    assert body["parameters"]["speaker_count"] == 3


def test_diarization_absent_when_disabled() -> None:
    body = body_for(make_provider(), diarization=False)
    assert "diarization_enabled" not in body["parameters"]


def test_language_hints_capped_at_four() -> None:
    body = body_for(make_provider(), language_hints=("zh", "en", "ja", "ko", "fr"))
    assert body["parameters"]["language_hints"] == ["zh", "en", "ja", "ko"]


def test_keep_dialect_only_for_31() -> None:
    assert body_for(make_provider(), keep_dialect=True)["parameters"]["keep_dialect"] is True
    assert (
        "keep_dialect"
        not in body_for(make_provider("qwen-audio-3.0-asr-flash-filetrans"), keep_dialect=True)["parameters"]
    )


# --- HTTP behaviour -----------------------------------------------------


def test_submit_sends_async_and_oss_resolve_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"output": {"task_id": "t-1", "task_status": "PENDING"}, "request_id": "r"}
        )

    provider = BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model="qwen-audio-3.1-asr-flash-filetrans",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    submitted = provider.submit(AsrRequest(file_url="oss://b/a.mp3", model=provider._model))

    assert submitted.task_id == "t-1"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-dashscope-async"] == "enable"
    # Without this header Bailian cannot resolve oss:// and the subtask fails.
    assert headers["x-dashscope-ossresourceresolve"] == "enable"
    assert captured["url"].endswith("/services/audio/asr/transcription")


def test_submit_without_task_id_raises() -> None:
    provider = BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"output": {}}))
        ),
    )
    from animatter.errors import AppError

    with pytest.raises(AppError, match="task_id"):
        provider.submit(AsrRequest(file_url="oss://b/a.mp3", model="m"))


def test_poll_reports_subtask_failure() -> None:
    payload = {
        "output": {
            "task_status": "SUCCEEDED",
            "results": [
                {
                    "subtask_status": "FAILED",
                    "code": "FILE_DOWNLOAD_FAILED",
                    "message": "cannot download",
                }
            ],
        }
    }
    provider = BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))),
    )
    status = provider.poll("t-1")
    assert status.terminal
    assert status.error_code == "FILE_DOWNLOAD_FAILED"


def test_poll_running_is_not_terminal() -> None:
    provider = BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
            )
        ),
    )
    assert provider.poll("t").terminal is False


def test_fetch_result_omits_authorization() -> None:
    """A signed OSS URL rejects an Authorization header."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"transcripts": [], "properties": {}})

    provider = BailianAsrProvider(
        base_url="https://example.test/api/v1",
        api_key="sk-test",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.fetch_result("https://oss.test/result.json?Signature=x")
    assert "authorization" not in seen


# --- parsing ------------------------------------------------------------


def test_parse_result_extracts_words_and_speakers() -> None:
    payload = {
        "properties": {"original_duration_in_milliseconds": 5000},
        "transcripts": [
            {
                "channel_id": 0,
                "text": "你好世界",
                "sentences": [
                    {
                        "begin_time": 100,
                        "end_time": 900,
                        "text": "你好",
                        "speaker_id": 1,
                        "words": [{"begin_time": 100, "end_time": 900, "text": "你好", "punctuation": "，"}],
                    }
                ],
            }
        ],
    }
    parsed = parse_result(payload)
    assert parsed.duration_ms == 5000
    sentence = parsed.channels[0].sentences[0]
    assert sentence.speaker_id == 1
    assert sentence.words[0].punctuation == "，"


def test_parse_result_tolerates_missing_fields() -> None:
    parsed = parse_result({})
    assert parsed.duration_ms == 0
    assert parsed.channels == []


def test_parse_result_without_speaker_id() -> None:
    payload = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "begin_time": 0,
                        "end_time": 100,
                        "text": "x",
                        "words": [{"text": "x", "begin_time": 0, "end_time": 100}],
                    }
                ]
            }
        ]
    }
    sentence = parse_result(payload).channels[0].sentences[0]
    assert sentence.speaker_id is None
