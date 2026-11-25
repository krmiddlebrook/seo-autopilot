"""Shared HTTP client. Tests inject an httpx.MockTransport through `make_client(transport=...)`."""

from __future__ import annotations

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; seo-autopilot/0.1; +https://github.com/)"


def make_client(transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )
