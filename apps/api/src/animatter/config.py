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

    # --- video export ---------------------------------------------------
    #: How many material segments one ffmpeg process may hold (§19.27).
    #:
    #: This is a *memory* limit, not a speed one. The filtergraph spends one branch per
    #: segment, ffmpeg is push-based, so every branch that is not yet current still receives
    #: frames. Measured on the 68-segment 费那奇MV project: 6 branches peak at 1.2 GB, 20 at
    #: 2.7 GB, 40 at 5.2 GB, 68 at 9.2-10.1 GB — where the machine's 15.9 GB runs out and
    #: ffmpeg dies with `Cannot allocate memory`. Six keeps a chunk near 1.2 GB.
    video_chunk_segments: int = 6
    #: How many seconds of film one ffmpeg process may encode.
    #:
    #: Segments are never split, so one long segment is one chunk however big it is; this
    #: bounds everything else. `费那奇MV` has a 591 s 全屏 clip, which is why the default is
    #: generous: 240 s would not have cut it, and cutting it is not possible without
    #: re-encoding the same material twice.
    video_chunk_seconds: int = 600
    #: Parallel ffmpeg processes during an export.
    #:
    #: One, and this is a *memory* bound rather than a speed one. The 1.2 GB figure above is not
    #: what a chunk costs at 1080p24 in practice: measured on this project, chunk 6 (6 materials,
    #: 204 s) peaks at 3.70 GB and chunk 8 (6 materials, 312 s, two 300 MB video sources) at
    #: 3.44 GB — the filtergraph's frame queues, not the branch count, dominate once the canvas is
    #: 1920x1080 RGBA. Three workers therefore ask for ~10 GB on a 15.9 GB machine that also runs
    #: the API and the browser, and the export died at 46% with ffmpeg `exit -12 Cannot allocate
    #: memory` (chunk 8's PNG decoder failing to get a buffer). One worker uses ~4 GB peak and
    #: takes ~25 min for this 61-minute film instead of ~15.
    #:
    #: Raising it is reasonable on a machine with more RAM; it is not a knob to turn up because
    #: the CPU has cores.
    video_chunk_workers: int = 1
    #: How many threads ffmpeg may slice the filtergraph across (§19.28).
    #:
    #: This is the **memory** knob, and the biggest single one. ffmpeg defaults to one slice per
    #: logical CPU, and each slice carries its own framesync queue; a queued 1920x1080 RGBA frame
    #: is 8.3 MB, so 16 slices ask for gigabytes. Measured on chunk 2 (95 s, 6 still segments):
    #: auto/16 died with `Cannot allocate memory` at 5.6-6.1 GB, 8 peaked at 6.26 GB, 4 at
    #: 5.95-6.22 GB, 2 at 4.71-4.82 GB, 1 at 3.58-3.81 GB.
    #:
    #: Four rather than one because the time cost is not linear — 1 thread doubles the encode
    #: (137.8 s vs 93.3 s at two) — and because with still pre-compositing (§19.28) the graph has
    #: only a handful of filters left, so four slices are enough to keep the CPUs busy.
    video_filter_threads: int = 4
    #: Pre-composite each still material segment into one canvas-sized PNG before encoding.
    #:
    #: The filtergraph used to redraw a static picture 24 times a second: boxblur (radius 20,
    #: power 2) on 1440x810, then scale/overlay/format/pad up to the canvas. Measured on chunk 2,
    #: the filtergraph was 88 % of the encode and x264 only 12 %. Rendering each still once and
    #: looping the PNG drops that chunk from 69-93 s / 3.6-4.7 GB to 18-21 s / 2.2 GB.
    #:
    #: Kept as a switch, not hard-wired: an export whose stills come out wrong has one setting to
    #: flip to get the old per-frame path back byte for byte.
    video_still_composite: bool = True
    #: Bake the static plate — poster + frame stroke + editable text — into one PNG before
    #: encoding, and carry the text pixels that sit under an avatar into that avatar's own PNG
    #: (§19.30, E1).
    #:
    #: The graph used to composite those layers on every frame. Bench, on a 100 s equivalent
    #: timeline: 53.48 → 33.86 s (−36.7 %), peak RSS 3.04 → 2.31 GB. On the real 3695.9 s film:
    #: 1670.7 → 1167.4 s (27.84 → 19.46 min, −30.1 %). Both at a frame-by-frame PSNR of
    #: **``inf``** against the old path — the two mp4s are byte-identical. (−36.7 % is the upper
    #: bound: that timeline is 72 % stills, the real film is 58.4 %; budget ~30 %.)
    #: Text goes in only when two template-level preconditions hold — no text inside the material
    #: box, and every avatar pixel under a text pixel fully opaque — which is checked **per bake**
    #: (the text is user-editable, so a compile-time answer goes stale), falling back to poster +
    #: stroke only (−16.8 %, still ``inf``).
    #:
    #: Kept as a switch for the same reason as ``video_still_composite``: an export whose plate
    #: comes out wrong has one setting to flip to get the old per-frame path back byte for byte.
    video_plate_bake: bool = True

    # --- Aliyun Bailian ------------------------------------------------
    dashscope_api_key: str | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
    dashscope_workspace_id: str | None = Field(default=None, validation_alias="DASHSCOPE_WORKSPACE_ID")
    dashscope_region_host: str = Field(
        default="dashscope.aliyuncs.com", validation_alias="DASHSCOPE_REGION_HOST"
    )
    asr_model: str = "qwen-audio-3.1-asr-flash-filetrans"
    #: Proof-reading and outline extraction. qwen-plus is enough for
    #: "find what looks wrong" work and much cheaper than the big models.
    llm_model: str = "qwen-plus"
    #: Default instant-hotword weight. 5 was measured to corrupt correct
    #: words (AGENTS.md §6.1), so 3 is the deliberate default.
    hotword_default_weight: int = 3

    # --- media limits (see AGENTS.md §2) -------------------------------
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
    def dashscope_compatible_base(self) -> str:
        """OpenAI-compatible endpoint root.

        This sits at the **domain root** as ``/compatible-mode/v1``, not under
        ``/api/v1`` — the two APIs use different prefixes on the same host, and
        reusing :attr:`dashscope_api_base` here returns 404.
        """
        return f"https://{self.dashscope_region_host}/compatible-mode/v1"

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
