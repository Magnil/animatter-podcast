"""Application settings.

Every tunable is environment-driven with the ``AM_`` prefix; the three
DashScope values keep their canonical names because that is what the operator
already has in ``.env`` and in their shell history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


def _find_env_files() -> tuple[str, ...]:
    """Locate ``.env`` by walking up from the working directory.

    The service runs from ``apps/api`` during development and from the repo root
    in scripts, so a single relative path would silently miss the key and fail
    with a confusing "unconfigured" error.
    """
    candidates: list[str] = []
    start = Path.cwd().resolve()
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            candidates.append(str(candidate))
            break
    return tuple(candidates)


class Settings(BaseSettings):
    """Runtime configuration for the local API process."""

    model_config = SettingsConfigDict(
        env_prefix="AM_",
        env_file=_find_env_files() or (".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- process -------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8317
    log_level: str = "INFO"
    data_dir: Path = Path("data")

    # --- external tools -------------------------------------------------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # --- Aliyun Bailian ------------------------------------------------
    dashscope_api_key: str | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
    dashscope_workspace_id: str | None = Field(default=None, validation_alias="DASHSCOPE_WORKSPACE_ID")
    dashscope_region_host: str = Field(
        default="dashscope.aliyuncs.com", validation_alias="DASHSCOPE_REGION_HOST"
    )
    asr_model: str = "qwen-audio-3.1-asr-flash-filetrans"

    # --- media limits (see AGENT.md §2) -------------------------------
    max_upload_bytes: int = 1_000_000_000  # Bailian temporary storage hard limit
    max_asr_seconds: int = 12 * 3600
    diarization_safe_seconds: int = 2 * 3600
    asr_chunk_target_seconds: int = 90 * 60

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        """The service holds unauthenticated access to local media; never expose it."""
        if value not in LOOPBACK_HOSTS:
            raise ValueError(
                f"host must be loopback ({', '.join(sorted(LOOPBACK_HOSTS))}), got {value!r}. "
                "Binding a public interface would expose the media store without authentication."
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level {value!r}")
        return level

    # --- derived paths --------------------------------------------------
    @property
    def dashscope_api_base(self) -> str:
        """Region base URL.

        A workspace-dedicated host (``<id>.cn-beijing.maas.aliyuncs.com``) is
        preferred over the shared ``dashscope.aliyuncs.com`` because Aliyun
        recommends it for isolation and stability; both accept the same calls.
        """
        return f"https://{self.dashscope_region_host}/api/v1"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def database_url(self) -> str:
        # SQLAlchemy needs forward slashes and an absolute path on Windows.
        return f"sqlite+pysqlite:///{self.db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """Build settings, applying explicit overrides last (used by tests)."""
    return Settings(**overrides)  # type: ignore[arg-type]
