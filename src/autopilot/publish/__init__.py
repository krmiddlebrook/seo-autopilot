"""CMS adapters. Each takes a stored article row and returns where it landed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..config import Config


@dataclass
class PublishResult:
    remote_id: str
    url: str
    status: str  # the status the CMS actually applied
    duplicate: bool = False
    slug_conflict: bool = False
    image_url: str = ""
    git_error: str = ""  # set when the file was saved and committed but could not be pushed


@dataclass
class RemoteDoc:
    """A page that already exists on the CMS, fetched so a refresh can edit it."""

    remote_id: str
    url: str
    title: str  # the SEO title (falls back to the H1 when the CMS has none)
    description: str
    body: str
    format: str  # markdown | html
    version: str = ""  # modified time / updated_at, sent back so the CMS can refuse a stale update
    h1: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    def publish(self, article: dict[str, Any], status: str) -> PublishResult: ...


class Updater(Protocol):
    """Publishers that can edit an existing post: WordPress, Markdown, Ghost and Shopify."""

    def fetch(self, url: str) -> RemoteDoc: ...

    def update(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult: ...


def can_update(publisher: Any) -> bool:
    return publisher is not None and callable(getattr(publisher, "fetch", None)) and callable(getattr(publisher, "update", None))


def slug_from_url(url: str) -> str:
    path = urlparse(url).path if "://" in url else url
    return path.rstrip("/").rsplit("/", 1)[-1]


def make_publisher(cfg: Config, client: httpx.Client) -> Publisher | None:
    target = cfg.publish.target.lower()
    if target == "none":
        return None
    if target == "wordpress":
        from .wordpress import WordPressPublisher

        return WordPressPublisher(cfg, client)
    if target in ("markdown", "mdx", "nextjs"):
        from .markdown import MarkdownPublisher

        return MarkdownPublisher(cfg)
    if target == "ghost":
        from .ghost import GhostPublisher

        return GhostPublisher(cfg, client)
    if target == "shopify":
        from .shopify import ShopifyPublisher

        return ShopifyPublisher(cfg, client)
    if target == "webhook":
        from .webhook import WebhookPublisher

        return WebhookPublisher(cfg, client)
    raise ValueError(f"Unknown publish target '{cfg.publish.target}'")
