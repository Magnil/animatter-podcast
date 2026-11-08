"""Bailian asynchronous file-transcription provider.

Covers ``qwen-audio-*-asr-flash-filetrans``, ``fun-asr`` and ``paraformer-v2``:
they share the submit/poll/download shape and differ only in which parameter
gates each capability (AGENTS.md §2.1/§2.2).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from animatter.errors import AppError
from animatter.log import get_logger
from animatter.providers.asr.base import (
    AsrCapabilities,
    AsrChannel,
    AsrProvider,
    AsrRequest,
    AsrResult,
    AsrSentence,
    AsrWord,
    SubmitResult,
    TaskStatus,
)

log = get_logger(__name__)

#: Model → capability table. Unknown models fall back to the conservative entry.
_CAPABILITIES: dict[str, AsrCapabilities] = {
    "qwen-audio-3.1-asr-flash-filetrans": AsrCapabilities(
        instant_vocabulary=True,
        precompiled_vocabulary=True,
        context=True,
        diarization=True,
        super_hotword_weight=True,
        max_hotwords=2000,
    ),
    "qwen-audio-3.0-asr-flash-filetrans": AsrCapabilities(
        instant_vocabulary=True,
        precompiled_vocabulary=True,
        context=True,
        diarization=True,
        super_hotword_weight=True,
        max_hotwords=2000,
    ),
    "fun-asr": AsrCapabilities(
        instant_vocabulary=True,
        precompiled_vocabulary=True,
        context=False,
        diarization=True,
        max_hotwords=2000,
    ),
    "paraformer-v2": AsrCapabilities(
        instant_vocabulary=False,
        precompiled_vocabulary=True,
        context=False,
        diarization=True,
        max_hotwords=500,
    ),
}

_FALLBACK = AsrCapabilities()

#: Poll cadence: start responsive, back off to a ceiling that respects the
#: documented 20 QPS query limit without wasting the quota.
_POLL_SCHEDULE = (2.0, 3.0, 5.0, 8.0, 10.0)


class BailianAsrProvider(AsrProvider):
    """Async file transcription against a Bailian workspace host."""

    name = "bailian"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=300.0), trust_env=False)
        self._owns_client = client is None

    @property
    def capabilities(self) -> AsrCapabilities:
        return _CAPABILITIES.get(self._model, _FALLBACK)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- protocol ---------------------------------------------------------

    def submit(self, request: AsrRequest) -> SubmitResult:
        body = self._build_body(request)
        response = self._client.post(
            f"{self._base_url}/services/audio/asr/transcription",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
                # Mandatory for oss:// URLs; without it Bailian treats the file
                # as unreachable and the subtask fails as FILE_DOWNLOAD_FAILED.
                "X-DashScope-OssResourceResolve": "enable",
            },
            json=body,
        )
        if response.status_code != 200:
            raise AppError(
                "asr_submit_failed",
                f"提交识别任务失败（HTTP {response.status_code}）",
                hint=_explain(response),
                status=502,
            )
        output = (response.json().get("output") or {}) if response.content else {}
        task_id = output.get("task_id")
        if not task_id:
            raise AppError(
                "asr_submit_no_task",
                "提交成功但没有返回 task_id",
                hint=f"响应：{response.text[:300]}",
                status=502,
            )
        log.info("asr.submitted", task_id=task_id, model=self._model)
        return SubmitResult(task_id=str(task_id), request_id=str(response.json().get("request_id", "")))

    def poll(self, task_id: str) -> TaskStatus:
        response = self._client.get(
            f"{self._base_url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise AppError(
                "asr_poll_failed",
                f"查询识别任务失败（HTTP {response.status_code}）",
                hint=_explain(response),
                status=502,
            )
        payload = response.json()
        output = payload.get("output") or {}
        state = str(output.get("task_status", "UNKNOWN"))
        results = output.get("results") or []
        code = ""
        message = ""
        for item in results:
            if str(item.get("subtask_status")) == "FAILED":
                code = str(item.get("code", ""))
                message = str(item.get("message", ""))
                break
        return TaskStatus(
            state=state,
            subtask_results=list(results),
            usage=dict(payload.get("usage") or {}),
            error_code=code,
            error_message=message,
        )

    def wait(self, task_id: str, *, timeout_seconds: int = 2700) -> TaskStatus:
        """Poll until terminal state or timeout.

        A timeout is *not* a failure: the task keeps running server-side and can
        be re-queried with the same id, so re-submitting would bill twice.
        """
        deadline = time.monotonic() + timeout_seconds
        attempt = 0
        while True:
            status = self.poll(task_id)
            if status.terminal:
                return status
            if time.monotonic() >= deadline:
                raise AppError(
                    "asr_timeout",
                    f"识别任务 {task_id} 超过 {timeout_seconds // 60} 分钟未完成",
                    hint="任务仍在服务端运行，可稍后用同一个 task_id 继续查询；不要重新提交，会重复计费。",
                    status=504,
                )
            delay = _POLL_SCHEDULE[min(attempt, len(_POLL_SCHEDULE) - 1)]
            attempt += 1
            time.sleep(delay)

    def fetch_result(self, transcription_url: str) -> AsrResult:
        # The URL is a plain signed OSS link — no Authorization header (sending
        # one makes OSS reject the signature as ambiguous).
        response = self._client.get(transcription_url)
        if response.status_code != 200:
            raise AppError(
                "asr_download_failed",
                f"下载识别结果失败（HTTP {response.status_code}）",
                hint="结果链接 24 小时失效；过期需要重新识别。",
                status=502,
            )
        return parse_result(response.json())

    # --- request assembly -------------------------------------------------

    def _build_body(self, request: AsrRequest) -> dict[str, Any]:
        caps = self.capabilities
        parameters: dict[str, Any] = {
            "channel_id": list(request.channel_id),
            "language_hints": list(request.language_hints[:4]),
        }

        if request.diarization and caps.diarization:
            parameters["diarization_enabled"] = True
            if request.speaker_count:
                parameters["speaker_count"] = request.speaker_count
        if request.keep_dialect and self._model.startswith("qwen-audio-3.1"):
            parameters["keep_dialect"] = True

        if request.vocabulary and caps.instant_vocabulary:
            parameters["vocabulary"] = dict(request.vocabulary)
        elif request.vocabulary:
            log.warning(
                "asr.instant_vocabulary_dropped",
                model=self._model,
                dropped=len(request.vocabulary),
                detail="该模型不支持即时热词，只有预编译热词会生效",
            )
        if request.vocabulary_id and caps.precompiled_vocabulary:
            parameters["vocabulary_id"] = request.vocabulary_id

        body: dict[str, Any] = {
            "model": request.model,
            "input": {"file_urls": [request.file_url]},
            "parameters": parameters,
        }

        if request.context_text and caps.context:
            body["input"]["context"] = [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.context_text[:400]}],
                }
            ]
        elif request.context_text:
            log.warning("asr.context_dropped", model=self._model)

        return body


def parse_result(payload: dict[str, Any]) -> AsrResult:
    """Normalise Bailian's result JSON, tolerating missing fields."""
    properties = payload.get("properties") or {}
    duration_ms = int(properties.get("original_duration_in_milliseconds") or 0)

    channels: list[AsrChannel] = []
    for transcript in payload.get("transcripts") or []:
        sentences: list[AsrSentence] = []
        for item in transcript.get("sentences") or []:
            words = [
                AsrWord(
                    text=str(word.get("text", "")),
                    start_ms=int(word.get("begin_time") or 0),
                    end_ms=int(word.get("end_time") or 0),
                    punctuation=str(word.get("punctuation", "")),
                )
                for word in item.get("words") or []
            ]
            sentences.append(
                AsrSentence(
                    start_ms=int(item.get("begin_time") or 0),
                    end_ms=int(item.get("end_time") or 0),
                    text=str(item.get("text", "")),
                    speaker_id=(int(item["speaker_id"]) if item.get("speaker_id") is not None else None),
                    words=words,
                )
            )
        channels.append(
            AsrChannel(
                channel_id=int(transcript.get("channel_id") or 0),
                sentences=sentences,
                full_text=str(transcript.get("text", "")),
            )
        )

    return AsrResult(duration_ms=duration_ms, channels=channels, raw=payload)


def _explain(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]

    code = str(body.get("code") or (body.get("output") or {}).get("code") or "")
    message = str(body.get("message") or (body.get("output") or {}).get("message") or "")
    known = {
        "InvalidApiKey": "API Key 无效，请确认使用的是北京地域且属于该工作空间的 Key。",
        "FILE_DOWNLOAD_FAILED": (
            "百炼下载不到这个文件：通常是 oss:// 链接过期（48h）或缺少 X-DashScope-OssResourceResolve 头。"
        ),
        "Throttling.RateQuota": "触发限流，稍后重试。",
        "InvalidParameter": "参数不合法，检查模型名与热词数量。",
    }
    hint = known.get(code, "")
    return f"{code}: {message} {hint}".strip() or json.dumps(body, ensure_ascii=False)[:300]
