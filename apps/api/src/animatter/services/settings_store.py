"""Persisted user settings, editable from the workbench menu.

Stored as a single JSON row rather than a column per option: the set of tunables
will keep growing, and a key-value blob means adding one is not a migration.
Defaults are applied on read, so an old file always loads.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from animatter.errors import AppError
from animatter.ids import utcnow_iso
from animatter.log import get_logger
from animatter.models import Setting

log = get_logger(__name__)

SETTINGS_ID = "singleton"

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
    "hotword_weight": (1, 50),
    "speaker_count": (1, 8),
    "proofread_chunk_size": (5, 50),
    "guard_ms": (0, 200),
    "min_keep_ms": (0, 1000),
    "fade_ms": (0, 50),
    "subtitle_cps": (4, 30),
    "subtitle_max_line": (8, 60),
    "subtitle_max_lines": (1, 4),
}
_ENUMS: dict[str, set[str]] = {
    "export_format": {"m4a", "wav"},
    "export_bitrate": {"128k", "192k", "256k", "320k"},
    "asr_model": {
        "qwen-audio-3.1-asr-flash-filetrans",
        "qwen-audio-3.0-asr-flash-filetrans",
        "fun-asr",
        "paraformer-v2",
    },
    "llm_model": {"qwen-plus", "qwen-max", "qwen-turbo"},
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
