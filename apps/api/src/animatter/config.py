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
    #: Ceiling on parallel ffmpeg processes. The actual count is decided per export by
    #: ``core.video_workers.plan_workers`` from **available memory** and the chunk shape (§19.34);
    #: this setting is the operator's override for a shared machine (set ``1`` to force serial).
    #:
    #: Why it is not a fixed number any more: measured on this project after the plate bake,
    #: a 6-segment chunk peaks at 2.54–2.87 GB whatever its material resolution, so two workers
    #: need ~8.3 GB of headroom while three would need ~11 GB on a 15.9 GB machine that also runs
    #: the API and a browser — which is what died at 46 % back then with ffmpeg `exit -12`.
    #: Two is the hard cap: three has never completed a run here (rounds 121 and 123 both OOMed).
    video_chunk_workers_max: int = 2
    #: How many threads ffmpeg may slice the filtergraph across (§19.28, §19.34).
    #:
    #: It used to be a **memory** knob: before still pre-compositing and the plate bake the graph
    #: queued a 1920x1080 RGBA frame per slice and 16 slices peaked at 5.6–6.1 GB, dying with
    #: `Cannot allocate memory`. Both of those layers are gone, and the peak is now flat across
    #: 4/8/12/16 slices (measured: chunk 2 2.62–2.67 GB, chunk 8 2.73–2.87 GB), so the knob is
    #: a **speed** one.
    #:
    #: Twelve, from an interleaved A/B (A,B,B,A, so drift cannot pose as a win): still-heavy
    #: chunk −15.0 %, 4K chunk −11.0 %, 591 s single-video chunk −8.6 %, whole film −6.6 %.
    #: Above 12 the gains stop (ft=16 is within 1 % of ft=12 and a hair worse on stills).
    video_filter_threads: int = 12
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
