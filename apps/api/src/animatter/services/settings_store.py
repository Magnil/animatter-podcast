"""Persisted user settings, editable from the workbench menu.

Stored as a single JSON row rather than a column per option: the set of tunables
will keep growing, and a key-value blob means adding one is not a migration.
Defaults are applied on read, so an old file always loads.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from animatter.core.fillers import DEFAULT_FILLERS, format_word_list
from animatter.errors import AppError
from animatter.ids import utcnow_iso
from animatter.log import get_logger
from animatter.models import Setting

log = get_logger(__name__)

SETTINGS_ID = "singleton"

#: Rendered once so the default value in DEFAULTS is a plain string, which the
#: settings UI edits directly.
FILLER_DEFAULTS = format_word_list(DEFAULT_FILLERS)

#: The authoritative defaults. The UI mirrors these to render an untouched form,
#: but this dict is what the server actually uses.
DEFAULTS: dict[str, Any] = {
    # -- models --
    "asr_model": "qwen-audio-3.1-asr-flash-filetrans",
    "llm_model": "qwen-plus",
    "hotword_weight": 3,
    "use_hotwords": False,
    # -- correction --
    "diarization": True,
    "speaker_count": 2,
    "use_proofread": True,
    "proofread_chunk_size": 25,
    # -- cleanup (one-click) --
    #: Filler list as one textarea value; parsed by core/fillers.parse_word_list.
    "cleanup_fillers": FILLER_DEFAULTS,
    "cleanup_min_silence_ms": 300,
    "cleanup_silence_pad_ms": 50,
    "cleanup_min_disfluency_ms": 300,
    # Detector settings. Separate from the cut threshold above on purpose:
    # silencedetect also decides where a *boundary* snaps, and those two
    # decisions want different values (see AGENTS notes in the cleanup doc).
    "silence_noise_db": -35,
    "silence_min_duration_ms": 300,
    # -- editing --
    "snap_to_silence": True,
    "guard_ms": 20,
    "min_keep_ms": 80,
    "fade_ms": 5,
    "realtime_skip": True,
    # -- export --
    "export_format": "m4a",  # m4a | wav
    "export_bitrate": "192k",
    "subtitle_cps": 9,
    "subtitle_max_line": 20,
    "subtitle_max_lines": 2,
    # -- text generation (导出 › ShowNotes / 文稿) --
    #: Rewrite style for 文稿. ShowNotes has no style: it is always a summary
    #: plus a timeline.
    "script_format": "dialogue",  # dialogue | article
    #: Appended verbatim to the end of every ShowNotes block. Multi-line on
    #: purpose — 「主播：xxx」 and a subscription line are two different lines,
    #: and forcing them onto one would make the signature unreadable.
    "shownotes_signature": "",
    #: Per-style prompt overrides. Empty means "use the built-in default", which
    #: is what lets a future prompt improvement reach users who never edited it.
    "script_prompt_shownotes": "",
    "script_prompt_dialogue": "",
    "script_prompt_article": "",
    #: Model parameters for document generation only. Editing and proof-reading
    #: keep their own hard-coded low temperature: those tasks want the boring
    #: answer, while a rewrite wants some latitude.
    "text_temperature": 0.6,
    "text_max_tokens": 8000,
}


class SettingsError(AppError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__("settings_invalid", message, hint=hint)


def _row(session: Session) -> Setting:
    row = session.get(Setting, SETTINGS_ID)
    if row is None:
        row = Setting(id=SETTINGS_ID, payload_json="{}", updated_at=utcnow_iso())
        session.add(row)
        session.flush()
    return row


def load(session: Session) -> dict[str, Any]:
    """Current settings: defaults overlaid with whatever has been saved."""
    stored: dict[str, Any] = {}
    try:
        stored = json.loads(_row(session).payload_json or "{}")
    except json.JSONDecodeError:
        log.warning("settings.corrupt", detail="falling back to defaults")
    return {**DEFAULTS, **stored}


def save(session: Session, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into settings.

    Unknown keys are rejected rather than stored: a typo in the client would
    otherwise persist silently and look like the setting "does not work".
    """
    unknown = sorted(set(patch) - set(DEFAULTS))
    if unknown:
        raise SettingsError(
            f"未知的设置项：{', '.join(unknown)}",
            "刷新页面后重试；如果仍失败，说明前后端版本不一致。",
        )

    validated = {key: _validate(key, value) for key, value in patch.items()}
    row = _row(session)
    current = load(session)
    merged = {**current, **validated}
    row.payload_json = json.dumps(merged, ensure_ascii=False)
    row.updated_at = utcnow_iso()
    session.flush()
    log.info("settings.saved", keys=sorted(validated))
    return merged


def reset(session: Session) -> dict[str, Any]:
    row = _row(session)
    row.payload_json = "{}"
    row.updated_at = utcnow_iso()
    session.flush()
    return dict(DEFAULTS)


_BOOLS = {
    "use_hotwords",
    "use_proofread",
    "diarization",
    "snap_to_silence",
    "realtime_skip",
}
_RANGES: dict[str, tuple[int, int]] = {
    "cleanup_min_silence_ms": (100, 3000),
    "cleanup_silence_pad_ms": (0, 300),
    "cleanup_min_disfluency_ms": (100, 3000),
    "silence_min_duration_ms": (100, 3000),
    "hotword_weight": (1, 50),
    "speaker_count": (1, 8),
    "proofread_chunk_size": (5, 50),
    "guard_ms": (0, 200),
    "min_keep_ms": (0, 1000),
    "fade_ms": (0, 50),
    "subtitle_cps": (4, 30),
    "subtitle_max_line": (8, 60),
    "subtitle_max_lines": (1, 4),
    "text_max_tokens": (1000, 32000),
}
_NEGATIVE_RANGES: dict[str, tuple[int, int]] = {
    # Detector threshold in dBFS: quieter than this counts as silence. ffmpeg's
    # own default is -35; a noisy room needs it higher (e.g. -30).
    "silence_noise_db": (-80, -10),
}
_ENUMS: dict[str, set[str]] = {
    "export_format": {"m4a", "wav"},
    "export_bitrate": {"128k", "192k", "256k", "320k"},
    "script_format": {"dialogue", "article"},
    "asr_model": {
        "qwen-audio-3.1-asr-flash-filetrans",
        "qwen-audio-3.0-asr-flash-filetrans",
        "fun-asr",
        "paraformer-v2",
    },
    "llm_model": {"qwen-plus", "qwen-max", "qwen-turbo"},
}

#: Free-text keys: prompts and the signature. Length-capped so a paste accident
#: cannot store a megabyte into the settings blob, which is read on every save.
_TEXT_KEYS: dict[str, int] = {
    "cleanup_fillers": 20_000,
    "shownotes_signature": 2_000,
    "script_prompt_shownotes": 8_000,
    "script_prompt_dialogue": 8_000,
    "script_prompt_article": 8_000,
}

#: Floats, with the range enforced. Temperature is the only one today, and the
#: range matters: 0 makes the rewrite deterministic and stilted, 2 makes it
#: hallucinate.
_FLOATS: dict[str, tuple[float, float]] = {
    "text_temperature": (0.0, 1.5),
}


def _validate(key: str, value: Any) -> Any:
    if key in _BOOLS:
        if not isinstance(value, bool):
            raise SettingsError(f"{key} 必须是 true/false")
        return value
    if key in _RANGES:
        low, high = _RANGES[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise SettingsError(f"{key} 必须是 {low}–{high} 之间的整数")
        return value
    if key in _NEGATIVE_RANGES:
        low, high = _NEGATIVE_RANGES[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise SettingsError(f"{key} 必须是 {low}–{high} 之间的整数（dBFS）")
        return value
    if key == "cleanup_fillers":
        if not isinstance(value, str):
            raise SettingsError("cleanup_fillers 必须是文本（每行一个词）")
        return value
    if key in _TEXT_KEYS:
        if not isinstance(value, str):
            raise SettingsError(f"{key} 必须是文本")
        limit = _TEXT_KEYS[key]
        if len(value) > limit:
            raise SettingsError(f"{key} 太长（上限 {limit} 字）")
        return value
    if key in _FLOATS:
        floor, ceiling = _FLOATS[key]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not floor <= float(value) <= ceiling
        ):
            raise SettingsError(f"{key} 必须是 {floor}–{ceiling} 之间的小数")
        return float(value)
    if key in _ENUMS:
        if not isinstance(value, str) or value not in _ENUMS[key]:
            raise SettingsError(f"{key} 只能是 {', '.join(sorted(_ENUMS[key]))} 之一")
        return value
    return value


def list_asr_models(session: Session) -> list[dict[str, Any]]:
    """Models with their capability flags, so the UI can explain trade-offs."""
    from animatter.providers.asr.bailian import _CAPABILITIES

    out: list[dict[str, Any]] = []
    for model in sorted(_ENUMS["asr_model"]):
        caps = _CAPABILITIES.get(model)
        out.append(
            {
                "id": model,
                "instant_vocabulary": bool(caps and caps.instant_vocabulary),
                "context": bool(caps and caps.context),
                "diarization": bool(caps and caps.diarization),
                "max_hotwords": caps.max_hotwords if caps else 0,
            }
        )
    return out


def get_setting(session: Session, key: str) -> Any:
    return load(session).get(key, DEFAULTS.get(key))
