"""How much memory is actually available right now (used to size the export's worker pool).

Why this is not in ``core/``: it is an OS query, and ``core/`` is pure functions only
(`AGENTS.md` 铁律). It is also not in ``services/`` because no transaction is involved — it is a
one-call probe that the media layer's caller needs before it decides how many ffmpeg processes
to start (§19.34).

**Windows first, because that is the machine that OOMs.** ``GlobalMemoryStatusEx().ullAvailPhys``
is the number Task Manager shows as 可用: free physical pages plus whatever can be reclaimed
from the standby list. ``psutil`` is not a dependency and is not worth adding for one call.

The Linux/macOS branch reads ``/proc/meminfo`` ``MemAvailable``, which is the same quantity.
It exists so the function has no "Windows only" claim it cannot keep — the export path is the
only caller, and a wrong number there means an OOM 20 minutes in, not a crash at startup.
"""

from __future__ import annotations

import sys
from pathlib import Path


def available_gb() -> float:
    """Available physical memory in GiB.

    Falls back to ``inf`` when it cannot be read: the caller's budget then allows the *maximum*
    number of workers, which is wrong but bounded by the hard cap. Returning 0 would silently
    force serial exports on a machine that has plenty of room, and that is the failure nobody
    would ever notice.
    """
    if sys.platform == "win32":
        value = _windows_available_bytes()
        return value / 2**30 if value else float("inf")
    value = _posix_available_bytes()
    return value / 2**30 if value else float("inf")


def _windows_available_bytes() -> int:
    import ctypes
    import ctypes.wintypes as wt

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", wt.DWORD),
            ("dwMemoryLoad", wt.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0
    except (AttributeError, OSError):
        return 0
    return int(status.ullAvailPhys)


def _posix_available_bytes() -> int:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return 0
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0
