"""Ghost Admin API. Auth is a short-lived HS256 JWT signed with the Admin API key's secret."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Config, secret
from . import PublishResult, RemoteDoc, slug_from_url


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ghost_token(admin_key: str, now: int | None = None) -> str:
    key_id, hex_secret = admin_key.split(":", 1)
    iat = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(bytes.fromhex(hex_secret), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


class GhostPublisher:
    def __init__(self, cfg: Config, client: httpx.Client):
        self.gc = cfg.publish.ghost
        self.client = client
        self.base = self.gc.url.rstrip("/") + "/ghost/api/admin"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Ghost {ghost_token(secret(self.gc.admin_key_env))}", "Accept-Version": "v5.0"}

    def upload_image(self, path: Path) -> str:
        with path.open("rb") as fh:
            r = self.client.post(f"{self.base}/images/upload/", headers=self._headers(),
                                 files={"file": (path.name, fh, "image/png")}, data={"purpose": "image"})
        r.raise_for_status()
        return r.json()["images"][0]["url"]

    def publish(self, article: dict[str, Any], status: str) -> PublishResult:
        existing = self.client.get(f"{self.base}/posts/slug/{article['slug']}/", headers=self._headers())
        if existing.status_code == 200:
            post = existing.json()["posts"][0]
            return PublishResult(post["id"], post.get("url", ""), post.get("status", ""), duplicate=True)
        image = article.get("image_url") or ""
        if not image and article.get("image_path"):
            image = self.upload_image(Path(article["image_path"]))
        schema = json.loads(article.get("schema_json") or "[]")
        post = {
            "title": article["h1"],
            "slug": article["slug"],
            "html": article["html"],
            "status": "published" if status == "publish" else "draft",
            "meta_title": article["meta_title"],
            "meta_description": article["meta_description"],
            "custom_excerpt": (article.get("excerpt") or "")[:300],
            "feature_image": image or None,
            "feature_image_alt": article.get("image_alt") or None,
            "tags": [{"name": t} for t in self.gc.tags],
            "codeinjection_head": "".join(
                f'<script type="application/ld+json">{json.dumps(s)}</script>' for s in schema
            ) or None,
        }
        r = self.client.post(f"{self.base}/posts/?source=html", headers=self._headers(), json={"posts": [post]})
        if r.status_code >= 400:
            raise RuntimeError(f"Ghost publish failed: HTTP {r.status_code} {r.text[:300]}")
        created = r.json()["posts"][0]
        return PublishResult(created["id"], created.get("url", ""), created.get("status", ""), image_url=image)

    # -- refresh an existing post ------------------------------------------
    def fetch(self, url: str) -> RemoteDoc:
        slug = slug_from_url(url)
        r = self.client.get(f"{self.base}/posts/slug/{slug}/", headers=self._headers(), params={"formats": "html"})
        if r.status_code == 404:
            raise LookupError(f"No Ghost post with slug '{slug}'")
        r.raise_for_status()
        post = r.json()["posts"][0]
        return RemoteDoc(
            remote_id=post["id"], url=post.get("url") or url, title=post.get("meta_title") or post.get("title", ""),
            description=post.get("meta_description") or post.get("custom_excerpt") or "",
            body=post.get("html") or "", format="html", version=post.get("updated_at", ""), h1=post.get("title", ""),
        )

    def update(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult:
        # Ghost rejects the write with a 409 when updated_at no longer matches: someone edited the post meanwhile
        post = {"html": body, "meta_title": title, "meta_description": description, "updated_at": doc.version}
        r = self.client.put(f"{self.base}/posts/{doc.remote_id}/?source=html", headers=self._headers(), json={"posts": [post]})
        if r.status_code >= 400:
            raise RuntimeError(f"Ghost update failed: HTTP {r.status_code} {r.text[:300]}")
        updated = r.json()["posts"][0]
        return PublishResult(updated["id"], updated.get("url") or doc.url, updated.get("status", ""))
