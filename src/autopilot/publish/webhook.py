"""Generic webhook: POST the full article as JSON, signed with HMAC-SHA256 when a secret is configured."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from ..config import Config, secret
from . import PublishResult

FIELDS = ("uid", "keyword", "slug", "h1", "meta_title", "meta_description", "excerpt", "markdown", "html",
          "image_url", "image_alt", "image_credit", "score")


class WebhookPublisher:
    def __init__(self, cfg: Config, client: httpx.Client):
        self.wc = cfg.publish.webhook
        self.client = client

    def publish(self, article: dict[str, Any], status: str) -> PublishResult:
        body = {k: article.get(k) for k in FIELDS}
        body["status"] = status
        body["schema"] = json.loads(article.get("schema_json") or "[]")
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": article["uid"]}
        if self.wc.secret_env:
            headers["X-Autopilot-Signature"] = "sha256=" + hmac.new(secret(self.wc.secret_env).encode(), raw, hashlib.sha256).hexdigest()
        r = self.client.post(self.wc.url, content=raw, headers=headers)
        r.raise_for_status()
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return PublishResult(str(data.get("id", "")), data.get("url", ""), data.get("status", status), duplicate=bool(data.get("duplicate")))
