"""Startup probes for the external binaries the pipeline cannot run without.

Failing fast here beats discovering a missing ffmpeg forty minutes into a
render (AGENT.md §15.6 item 31).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

_VERSION_RE = re.compile(r"version\s+(\d+)\.(\d+)")

#: Below this, ``-filter_complex_script`` and the atrim/concat graph are still
#: fine, but 7.x is what the project is developed against; older majors warn.
MINIMUM_MAJOR = 6

_INSTALL_HINT = (
    "Install ffmpeg and make sure `ffmpeg` is on PATH "
    "(Windows: winget install Gyan.FFmpeg, or drop the gyan.dev build into C:\\ffmpeg\\bin). "
    "Then set AM_FFMPEG_PATH / AM_FFPROBE_PATH if the binary lives elsewhere."
)


@dataclass(frozen=True)
class ToolStatus:
    name: str
    path: str
    available: bool
    version: str = ""
    major: int = 0
    minor: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "available": self.available,
            "version": self.version,
            "major": self.major,
            "minor": self.minor,
            "detail": self.detail,
        }


def _run_version(binary: str, name: str) -> ToolStatus:
    resolved = shutil.which(binary)
    if resolved is None:
        return ToolStatus(name=name, path=binary, available=False, detail=_INSTALL_HINT)
    try:
        completed = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        return ToolStatus(name=name, path=resolved, available=False, detail=f"{type(exc).__name__}: {exc}")

    first_line = (
        (completed.stdout or completed.stderr or "").splitlines()[0]
        if completed.stdout or completed.stderr
        else ""
    )
    match = _VERSION_RE.search(first_line)
    if completed.returncode != 0 or match is None:
        return ToolStatus(
            name=name,
            path=resolved,
            available=False,
            detail=f"`{resolved} -version` failed (exit {completed.returncode}): {first_line[:160]}",
        )

    major, minor = int(match.group(1)), int(match.group(2))
    detail = "" if major >= MINIMUM_MAJOR else f"ffmpeg {major}.{minor} is older than {MINIMUM_MAJOR}.0"
    return ToolStatus(
        name=name,
        path=resolved,
        available=True,
        version=f"{major}.{minor}",
        major=major,
        minor=minor,
        detail=detail,
    )


@lru_cache(maxsize=8)
def probe_media_tools(ffmpeg_path: str, ffprobe_path: str) -> tuple[ToolStatus, ToolStatus]:
    """Probe both binaries; cached so /healthz stays cheap."""
    return _run_version(ffmpeg_path, "ffmpeg"), _run_version(ffprobe_path, "ffprobe")
