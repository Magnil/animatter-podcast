"""Health and readiness reporting.

``/healthz`` answers "can this process actually do work?" — not just "is the
HTTP server up". A missing ffmpeg is a 503 here so the UI can show the install
hint instead of failing at render time.
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from animatter.log import get_logger
from animatter.media.probe import probe_media_tools

router = APIRouter()
log = get_logger(__name__)

MIN_FREE_BYTES = 2 * 1024**3


@router.get("/healthz")
def healthz(request: Request, response: Response) -> dict[str, Any]:
    settings = request.app.state.settings
    db = request.app.state.db

    checks: dict[str, Any] = {}

    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "path": str(settings.db_path)}
    except (sqlite3.Error, OSError) as exc:
        checks["database"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ffmpeg, ffprobe = probe_media_tools(settings.ffmpeg_path, settings.ffprobe_path)
    checks["ffmpeg"] = {**ffmpeg.as_dict(), "ok": ffmpeg.available}
    checks["ffprobe"] = {**ffprobe.as_dict(), "ok": ffprobe.available}

    try:
        usage = shutil.disk_usage(settings.data_dir)
        free = usage.free
        checks["disk"] = {
            "ok": free >= MIN_FREE_BYTES,
            "free_gb": round(free / 1024**3, 2),
            "data_dir": str(settings.data_dir),
            "detail": ""
            if free >= MIN_FREE_BYTES
            else f"剩余空间不足 {MIN_FREE_BYTES // 1024**3}GB，渲染会失败",
        }
    except OSError as exc:
        checks["disk"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    checks["asr_credentials"] = {
        "ok": bool(settings.dashscope_api_key),
        "detail": ""
        if settings.dashscope_api_key
        else "未配置 DASHSCOPE_API_KEY，转写功能不可用（其余功能正常）",
    }

    required = ("database", "ffmpeg", "ffprobe", "disk")
    healthy = all(checks[name]["ok"] for name in required)
    if not healthy:
        response.status_code = 503
        log.warning("healthz.degraded", checks=checks)

    return {
        "status": "ok" if healthy else "degraded",
        "version": request.app.state.version,
        "checks": checks,
    }
