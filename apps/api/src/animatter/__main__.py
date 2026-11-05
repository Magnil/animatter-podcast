"""CLI entry point: ``animatter serve|check|db-upgrade|version``."""

from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from animatter.config import load_settings
from animatter.log import configure_logging
from animatter.media.probe import probe_media_tools


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "animatter.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_config=None,
    )
    return 0


def _cmd_check(_args: argparse.Namespace) -> int:
    from animatter.main import VERSION

    settings = load_settings()
    ffmpeg, ffprobe = probe_media_tools(settings.ffmpeg_path, settings.ffprobe_path)
    report = {
        "version": VERSION,
        "data_dir": str(settings.data_dir.resolve()),
        "database": str(settings.db_path.resolve()),
        "dashscope_key": "configured" if settings.dashscope_api_key else "missing",
        "ffmpeg": ffmpeg.as_dict(),
        "ffprobe": ffprobe.as_dict(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = ffmpeg.available and ffprobe.available
    if not ok:
        print("\n媒体工具不可用，转写与剪辑无法运行。", file=sys.stderr)
    return 0 if ok else 1


def _cmd_db_upgrade(_args: argparse.Namespace) -> int:
    from animatter.migrate import upgrade_to_head

    settings = load_settings()
    configure_logging(settings.log_level)
    upgrade_to_head(settings)
    print(f"migrated: {settings.db_path.resolve()}")
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from animatter.cli_transcribe import run

    vocabulary = json.loads(args.vocabulary) if args.vocabulary else None
    return run(
        project_name=args.project,
        audio=Path(args.audio),
        model=args.model,
        vocabulary=vocabulary,
        no_diarization=args.no_diarization,
        json_out=args.json,
    )


def _cmd_version(_args: argparse.Namespace) -> int:
    from animatter.main import VERSION

    print(VERSION)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="animatter", description="Animatter Podcast Studio")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the local API server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    check = sub.add_parser("check", help="verify toolchain and configuration")
    check.set_defaults(func=_cmd_check)

    db_upgrade = sub.add_parser("db-upgrade", help="apply database migrations")
    db_upgrade.set_defaults(func=_cmd_db_upgrade)

    transcribe = sub.add_parser("transcribe", help="run the full transcription chain on one audio file")
    transcribe.add_argument("audio", help="path to the audio file")
    transcribe.add_argument("--project", required=True, help="project name to create")
    transcribe.add_argument("--model", default=None, help="override the ASR model")
    transcribe.add_argument(
        "--vocabulary", default=None, help="instant hotwords as JSON, e.g. '{\"张三\":4}'"
    )
    transcribe.add_argument("--no-diarization", action="store_true", help="disable speaker separation")
    transcribe.add_argument("--json", action="store_true", help="emit JSON logs")
    transcribe.set_defaults(func=_cmd_transcribe)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=_cmd_version)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
