"""Provider protocols.

Capabilities differ by model (AGENTS.md §2.2): ``paraformer-v2`` accepts a
pre-compiled ``vocabulary_id`` but not instant ``vocabulary`` or ``context``;
``qwen3-asr-flash-filetrans`` accepts neither. Services must branch on these
declarations rather than on the model name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AsrCapabilities:
    instant_vocabulary: bool = False
    precompiled_vocabulary: bool = False
    context: bool = False
    diarization: bool = False
    super_hotword_weight: bool = False
    max_hotwords: int = 500
    max_duration_seconds: int = 12 * 3600


@dataclass(frozen=True)
class AsrRequest:
    """Everything one transcription call needs."""

    file_url: str
    model: str
    language_hints: tuple[str, ...] = ("zh", "en")
    channel_id: tuple[int, ...] = (0,)
    diarization: bool = False
    speaker_count: int | None = None
    vocabulary: dict[str, int] = field(default_factory=dict)
    vocabulary_id: str | None = None
    context_text: str | None = None
    keep_dialect: bool = False


@dataclass(frozen=True)
class AsrWord:
    text: str
    start_ms: int
    end_ms: int
    punctuation: str = ""


@dataclass(frozen=True)
class AsrSentence:
    start_ms: int
    end_ms: int
    text: str
    speaker_id: int | None
    words: list[AsrWord]


@dataclass(frozen=True)
class AsrChannel:
    channel_id: int
    sentences: list[AsrSentence]
    full_text: str


@dataclass(frozen=True)
class AsrResult:
    duration_ms: int
    channels: list[AsrChannel]
    raw: dict[str, object]


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    request_id: str = ""


@dataclass(frozen=True)
class TaskStatus:
    state: str  # PENDING | RUNNING | SUCCEEDED | FAILED | CANCELED
    subtask_results: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, object] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in {"SUCCEEDED", "FAILED", "CANCELED"}


class AsrProvider(Protocol):
    """One provider implementation per model family."""

    name: str

    @property
    def capabilities(self) -> AsrCapabilities: ...

    def submit(self, request: AsrRequest) -> SubmitResult: ...

    def poll(self, task_id: str) -> TaskStatus: ...

    def fetch_result(self, transcription_url: str) -> AsrResult: ...


class MediaTool(Protocol):
    """Narrow view of the ffmpeg helpers so providers stay testable."""

    def transcode_for_asr(self, src: Path, dst: Path) -> Path: ...
