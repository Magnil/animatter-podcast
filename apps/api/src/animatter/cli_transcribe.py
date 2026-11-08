"""CLI: ``animatter transcribe`` — the M1 acceptance path.

Runs the full chain without a browser so the network/API surface can be proven
in isolation (AGENTS.md §13: "M1 之前不要动前端").
"""

from __future__ import annotations

import json
from pathlib import Path

from animatter.config import load_settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import configure_logging, get_logger
from animatter.migrate import upgrade_to_head
from animatter.services import transcribe as transcribe_service

log = get_logger(__name__)


def run(
    *,
    project_name: str,
    audio: Path,
    model: str | None = None,
    vocabulary: dict[str, int] | None = None,
    no_diarization: bool = False,
    json_out: bool = False,
) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, json_logs=json_out)
    upgrade_to_head(settings)

    db = Database(settings)
    try:
        with db.session() as session:
            from animatter.services import projects as project_service

            project = project_service.create_project(session, settings, project_name)
            print(f"project  {project.id}  {project.name}")

            media = transcribe_service.register_media(session, settings, project.id, audio)
            print(f"media    {media.id}  {media.duration_ms / 1000:.1f}s  {media.channels}ch")

            options = transcribe_service.TranscribeOptions(
                model=model or settings.asr_model,
                diarization=not no_diarization,
                vocabulary=vocabulary,
            )

            def progress(fraction: float, message: str) -> None:
                print(f"  [{fraction * 100:5.1f}%] {message}", flush=True)

            version = transcribe_service.transcribe_media(
                session, settings, project_id=project.id, media=media, options=options, progress=progress
            )

            tokens = transcribe_service.load_tokens(session, version.id)
            sentences = transcribe_service.load_sentences(session, version.id)
            derived = sum(1 for token in tokens if token.derived)

            summary = {
                "project_id": project.id,
                "version_id": version.id,
                "duration_ms": version.duration_ms,
                "sentences": len(sentences),
                "tokens": len(tokens),
                "derived_ratio": round(derived / len(tokens), 3) if tokens else 0.0,
                "speech_end_ms": max((t.end_ms for t in tokens), default=0),
                "first_sentences": [
                    "".join(t.text + t.punc_after for t in tokens[s.token_start : s.token_end])
                    for s in sentences[:5]
                ],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except AppError as exc:
        print(f"\n失败：{exc.code}\n  {exc.message}")
        if exc.hint:
            print(f"  建议：{exc.hint}")
        return 2
    finally:
        db.dispose()
