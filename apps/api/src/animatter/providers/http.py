"""HTTP client shared by every Aliyun call.

Two details that must not be repeated per-call-site:

* ``trust_env=False`` by default. The host network here sets ``ALL_PROXY`` to a
  SOCKS URL, which httpx cannot parse ("Invalid port"), so inheriting the
  environment breaks every request. Aliyun domains are reached directly anyway.
* Retries honour ``Retry-After`` and never retry a 4xx that will not change.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=300.0, pool=10.0)
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def build_client(*, timeout: httpx.Timeout | None = None, trust_env: bool = False) -> httpx.Client:
    """Create a client with sane pooling for long uploads and polling."""
    return httpx.Client(
        timeout=timeout or DEFAULT_TIMEOUT,
        trust_env=trust_env,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        headers={"User-Agent": "animatter-podcast/0.1"},
    )


async def build_async_client(
    *, timeout: httpx.Timeout | None = None, trust_env: bool = False
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout or DEFAULT_TIMEOUT,
        trust_env=trust_env,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        headers={"User-Agent": "animatter-podcast/0.1"},
    )


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS
