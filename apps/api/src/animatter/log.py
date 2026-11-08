"""Structured logging with secret redaction.

Redaction is not optional: request bodies and OSS URLs pass through log calls,
and both can carry credentials or private media paths (AGENTS.md §11).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(signature=)[^&\s]+"),
    re.compile(r"(?i)(ossaccesskeyid=)[^&\s]+"),
)


def _scrub(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", value)
    return value


def _redact_processor(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = _scrub(value)
        elif isinstance(value, dict):
            event_dict[key] = {k: (_scrub(v) if isinstance(v, str) else v) for k, v in value.items()}
    return event_dict


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Install structlog on top of stdlib logging, once per process."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level))

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
