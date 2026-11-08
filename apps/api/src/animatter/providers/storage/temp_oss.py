"""Bailian temporary OSS upload (``getPolicy`` → multipart POST → ``oss://`` URL).

The three rules that make this fail confusingly if violated (AGENTS.md §2.3):

1. The ``model`` used to fetch the policy must equal the model used for
   inference, or the uploaded object is simply not visible to the ASR call.
2. The policy expires in 300 seconds, so it is fetched immediately before the
   upload rather than reused.
3. ``oss://`` URLs require ``X-DashScope-OssResourceResolve: enable`` on the
   consuming request — that header belongs to the caller, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx

from animatter.errors import AppError


@dataclass(frozen=True)
class UploadPolicy:
    upload_host: str
    upload_dir: str
    oss_access_key_id: str
    signature: str
    policy: str
    x_oss_object_acl: str
    x_oss_forbid_overwrite: str
    expire_in_seconds: int
    max_file_size_mb: int


@dataclass(frozen=True)
class UploadedFile:
    oss_url: str
    key: str
    size_bytes: int


class BailianStorage:
    """Temporary storage client bound to one workspace host and model."""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def get_policy(self, client: httpx.Client) -> UploadPolicy:
        response = client.get(
            f"{self._base_url}/uploads",
            headers=self._headers(),
            params={"action": "getPolicy", "model": self._model},
        )
        if response.status_code != 200:
            raise AppError(
                "oss_policy_failed",
                "获取临时上传凭证失败",
                hint=_explain(response),
                status=502,
            )
        data = response.json().get("data") or {}
        return UploadPolicy(
            upload_host=str(data["upload_host"]),
            upload_dir=str(data["upload_dir"]),
            oss_access_key_id=str(data["oss_access_key_id"]),
            signature=str(data["signature"]),
            policy=str(data["policy"]),
            x_oss_object_acl=str(data.get("x_oss_object_acl", "private")),
            x_oss_forbid_overwrite=str(data.get("x_oss_forbid_overwrite", "true")),
            expire_in_seconds=int(data.get("expire_in_seconds", 300)),
            max_file_size_mb=int(data.get("max_file_size_mb", 1024)),
        )

    def upload(self, client: httpx.Client, file_path: Path) -> UploadedFile:
        size = file_path.stat().st_size
        policy = self.get_policy(client)

        limit = policy.max_file_size_mb * 1024 * 1024
        if size > limit:
            raise AppError(
                "upload_too_large",
                f"文件 {size / 1024**2:.0f}MB 超过临时存储上限 {policy.max_file_size_mb}MB",
                hint=(
                    "先用 ffmpeg 转码为 16kHz 单声道 mp3（本项目会自动做这一步）；"
                    "若仍超限，说明源文件时长异常。"
                ),
            )

        # The object name must be ASCII-safe; Bailian keeps the original name only
        # for human inspection, and CJK filenames break OSS signature matching.
        safe_name = f"episode{file_path.suffix.lower() or '.mp3'}"
        key = f"{policy.upload_dir}/{safe_name}"

        # Streamed from a handle rather than read into memory: the ASR artifact
        # can be ~350MB for a 12h episode, and OSS accepts a file object.
        with file_path.open("rb") as handle:
            files: list[tuple[str, tuple[str | None, str | bytes | IO[bytes]]]] = [
                ("OSSAccessKeyId", (None, policy.oss_access_key_id)),
                ("Signature", (None, policy.signature)),
                ("policy", (None, policy.policy)),
                ("x-oss-object-acl", (None, policy.x_oss_object_acl)),
                ("x-oss-forbid-overwrite", (None, policy.x_oss_forbid_overwrite)),
                ("key", (None, key)),
                ("success_action_status", (None, "200")),
                # `file` must be the last form field.
                ("file", (safe_name, handle)),
            ]
            response = client.post(policy.upload_host, files=files)
        if response.status_code != 200:
            raise AppError(
                "oss_upload_failed",
                f"上传到临时存储失败（HTTP {response.status_code}）",
                hint=_explain(response),
                status=502,
            )
        return UploadedFile(oss_url=f"oss://{key}", key=key, size_bytes=size)


def _explain(response: httpx.Response) -> str:
    """Turn an Aliyun error body into an actionable sentence."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]

    code = str(body.get("code", ""))
    message = str(body.get("message", ""))
    known = {
        "InvalidApiKey": "API Key 无效：确认用的是北京地域、且与工作空间匹配的 Key。",
        "AccessDenied": "凭证已过期（有效期 300 秒）或权限不足；重新获取凭证后再试。",
        "Throttling.RateQuota": "触发限流（凭证接口 100 QPS）；稍后重试即可。",
        "InvalidParameter.ModelNotFound": "模型名不存在或该工作空间未开通；检查 AM_ASR_MODEL 配置。",
    }
    hint = known.get(code, "")
    base = f"{code}: {message}" if code or message else response.text[:300]
    return f"{base} {hint}".strip()
