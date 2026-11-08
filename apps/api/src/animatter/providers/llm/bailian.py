"""LLM access over Bailian's OpenAI-compatible endpoint.

Uses ``/compatible-mode/v1`` because it is one stable protocol across models,
so swapping ``qwen-plus`` for a bigger model is a config change rather than a
rewrite. The same workspace key and host serve it (AGENTS.md §2.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

#: Kept deliberately small: extraction and proof-reading are structured tasks,
#: not generation tasks, and a runaway completion is pure cost.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


@dataclass(frozen=True)
class LlmReply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str


class BailianLlm:
    """Minimal chat client. No retries beyond one transport retry — callers decide."""

    def __init__(self, *, base_url: str, api_key: str, model: str = "qwen-plus") -> None:
        """``base_url`` must be the compatible endpoint root, e.g. ``https://host/compatible-mode/v1``."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        *,
        system: str,
        user: str,
        client: httpx.Client,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        json_mode: bool = True,
    ) -> LlmReply:
        """One non-streaming completion.

        ``temperature=0.1`` by default: both extraction and proof-reading want
        the boring answer, and low temperature makes results reproducible enough
        to diff between runs.
        """
        body: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        response = client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if response.status_code != 200:
            raise AppError(
                "llm_call_failed",
                f"模型调用失败（HTTP {response.status_code}）",
                hint=_explain(response),
                status=502,
            )

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise AppError(
                "llm_empty",
                "模型没有返回内容",
                hint=f"原始响应：{json.dumps(payload, ensure_ascii=False)[:300]}",
                status=502,
            )
        usage = payload.get("usage") or {}
        reply = LlmReply(
            text=str(choices[0].get("message", {}).get("content", "")),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=str(payload.get("model") or self._model),
        )
        log.info(
            "llm.call",
            model=reply.model,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
        return reply

    def chat_json(
        self, *, system: str, user: str, client: httpx.Client, max_tokens: int = 2000
    ) -> tuple[dict[str, object], LlmReply]:
        """Call the model and parse the reply as a JSON object.

        Models occasionally wrap JSON in a ```json fence even in JSON mode, so
        the fence is stripped rather than treated as a hard failure.
        """
        reply = self.chat(system=system, user=user, client=client, max_tokens=max_tokens)
        parsed = parse_json_object(reply.text)
        if parsed is None:
            raise AppError(
                "llm_bad_json",
                "模型返回的不是合法 JSON",
                hint=f"原始输出前 300 字：{reply.text[:300]}",
                status=502,
            )
        return parsed, reply


def parse_json_object(text: str) -> dict[str, object] | None:
    """Best-effort extraction of one JSON object from model output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop the opening fence and the closing fence if present.
        if len(lines) >= 2:
            stripped = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
    stripped = stripped.strip()

    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None

    return value if isinstance(value, dict) else None


def _explain(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    code = str(body.get("code") or body.get("error", {}).get("code") or "")
    message = str(body.get("message") or body.get("error", {}).get("message") or "")
    known = {
        "InvalidApiKey": "API Key 无效，确认用的是北京地域、且开启了该模型的权限。",
        "Throttling.RateQuota": "触发限流，稍后重试或降低并发。",
        "ModelNotFound": "该工作空间未开通此模型，换一个模型或先在控制台开通。",
    }
    return f"{code}: {message} {known.get(code, '')}".strip() or response.text[:300]
