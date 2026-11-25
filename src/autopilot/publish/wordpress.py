"""WordPress via a Soro-compatible receiver plugin (ours, or Soro's GPL plugin), or core REST + Application Passwords.

Receiver mode gets idempotency (article id), the slug cannibalization guard, Yoast/Rank Math/AIOSEO meta and image
sideloading for free. Our `autopilot-receiver` plugin also accepts `meta_title` and `schema` (JSON-LD), which Soro's
payload has no field for, and `featured_image_base64` for locally generated images.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from ..config import Config, secret
from . import PublishResult, RemoteDoc, slug_from_url


class WordPressPublisher:
    def __init__(self, cfg: Config, client: httpx.Client):
        self.cfg = cfg
        self.wp = cfg.publish.wordpress
        self.client = client
        self.base = (self.wp.url or cfg.site.url).rstrip("/")
        self.is_soro = self.wp.namespace.startswith("soro")

    # -- receiver plugin -------------------------------------------------
    def _headers(self) -> dict[str, str]:
        key = secret(self.wp.api_key_env)
        return {"Authorization": f"Bearer {key}", "X-Soro-Key": key, "X-Autopilot-Key": key}

    def _route(self, name: str) -> str:
        return f"{self.base}/wp-json/{self.wp.namespace.strip('/')}/{name}"

    def verify(self) -> dict[str, Any]:
        if self.wp.mode == "core":
            r = self.client.get(f"{self.base}/wp-json/wp/v2/users/me", auth=self._basic())
        else:
            r = self.client.get(self._route("verify"), headers=self._headers())
        r.raise_for_status()
        return r.json()

    def setup_indexnow(self) -> dict[str, Any]:
        r = self.client.post(self._route("setup-indexnow"), headers=self._headers())
        r.raise_for_status()
        return r.json()

    def indexnow_status(self) -> dict[str, Any]:
        r = self.client.get(self._route("indexnow-status"), headers=self._headers())
        r.raise_for_status()
        return r.json()

    def payload(self, article: dict[str, Any], status: str, image_url: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": article["h1"],
            "content": article["html"],
            "status": status,
            "slug": article["slug"],
            "excerpt": article.get("excerpt") or "",
            "meta_description": article["meta_description"],
            "focus_keyword": article["keyword"],
            "soro_article_id": article["uid"],
        }
        if self.wp.category:
            body["category"] = self.wp.category
        if image_url:
            body["featured_image_url"] = image_url
        if not self.is_soro:
            body["meta_title"] = article["meta_title"]
            body["schema"] = json.loads(article.get("schema_json") or "[]")
            body["image_alt"] = article.get("image_alt") or article["h1"]
            if not image_url and article.get("image_path"):
                path = Path(article["image_path"])
                body["featured_image_base64"] = base64.b64encode(path.read_bytes()).decode()
                body["featured_image_filename"] = path.name
        return body

    def publish(self, article: dict[str, Any], status: str) -> PublishResult:
        if self.wp.mode == "core":
            return self._publish_core(article, status)
        image_url = article.get("image_url") or ""
        if not image_url and article.get("image_path") and (self.is_soro or self.wp.username):
            # Soro's plugin only sideloads from a URL, so push local files through the media endpoint first
            image_url = self.upload_media(Path(article["image_path"]), article.get("image_alt") or article["h1"])["source_url"]
        r = self.client.post(self._route("publish"), headers=self._headers(), json=self.payload(article, status, image_url), timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"WordPress publish failed: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        post = data.get("post", data)
        return PublishResult(
            remote_id=str(post.get("post_id") or post.get("id") or data.get("post_id") or ""),
            url=post.get("url") or post.get("post_url") or post.get("permalink") or post.get("link") or "",
            status=post.get("status") or data.get("status") or status,
            duplicate=bool(data.get("duplicate")),
            slug_conflict=bool(data.get("slug_conflict")),
            image_url=image_url,
        )

    # -- refresh an existing post ------------------------------------------
    def fetch(self, url: str) -> RemoteDoc:
        if self.wp.mode == "core":
            return self._fetch_core(url)
        if self.is_soro:
            raise RuntimeError("Soro's plugin can't edit existing posts. Install autopilot-receiver (wordpress-plugin/) "
                               "or set publish.wordpress.mode = \"core\" to refresh pages.")
        r = self.client.get(self._route("content"), headers=self._headers(), params={"url": url})
        if r.status_code == 404:
            raise LookupError(f"WordPress has no post at {url}")
        if r.status_code >= 400:
            raise RuntimeError(f"WordPress content fetch failed: HTTP {r.status_code} {r.text[:300]}")
        post = r.json()
        return RemoteDoc(
            remote_id=str(post["post_id"]), url=post.get("url") or url,
            title=post.get("meta_title") or post.get("title", ""), description=post.get("meta_description") or "",
            body=post.get("content") or "", format="html", version=post.get("modified", ""), h1=post.get("title", ""),
        )

    def update(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult:
        if self.wp.mode == "core":
            return self._update_core(doc, title, description, body)
        r = self.client.post(self._route("update"), headers=self._headers(), timeout=120, json={
            "post_id": int(doc.remote_id), "content": body, "meta_title": title, "meta_description": description,
            "expected_modified": doc.version,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"WordPress update failed: HTTP {r.status_code} {r.text[:300]}")
        post = r.json().get("post", r.json())
        return PublishResult(str(post.get("post_id") or doc.remote_id), post.get("url") or doc.url, post.get("status", ""))

    def _fetch_core(self, url: str) -> RemoteDoc:
        slug = slug_from_url(url)
        r = self.client.get(f"{self.base}/wp-json/wp/v2/posts", auth=self._basic(),
                            params={"slug": slug, "status": "any", "context": "edit"})
        r.raise_for_status()
        if not r.json():
            raise LookupError(f"WordPress has no post with slug '{slug}'")
        post = r.json()[0]
        meta = post.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        title = (post.get("title") or {}).get("raw", "")
        excerpt = (post.get("excerpt") or {}).get("raw", "")
        return RemoteDoc(
            remote_id=str(post["id"]), url=post.get("link") or url, title=meta.get("_yoast_wpseo_title") or title,
            description=meta.get("_yoast_wpseo_metadesc") or excerpt, body=(post.get("content") or {}).get("raw", ""),
            format="html", version=post.get("modified", ""), h1=title,
        )

    def _update_core(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult:
        auth = self._basic()
        payload: dict[str, Any] = {
            "content": body,
            "meta": {"_yoast_wpseo_metadesc": description, "_yoast_wpseo_title": title},
        }
        r = self.client.post(f"{self.base}/wp-json/wp/v2/posts/{doc.remote_id}", auth=auth, json=payload, timeout=120)
        if r.status_code == 400 and "meta" in r.text:
            payload.pop("meta")  # SEO keys not exposed to REST: the body still updates, the snippet can't
            r = self.client.post(f"{self.base}/wp-json/wp/v2/posts/{doc.remote_id}", auth=auth, json=payload, timeout=120)
        r.raise_for_status()
        post = r.json()
        return PublishResult(str(post["id"]), post.get("link") or doc.url, post.get("status", ""))

    # -- core REST (Application Passwords) -------------------------------
    def _basic(self) -> tuple[str, str]:
        if not (self.wp.username and self.wp.app_password_env):
            raise RuntimeError("Set publish.wordpress.username and app_password_env for core mode / media upload")
        return self.wp.username, secret(self.wp.app_password_env)

    def upload_media(self, path: Path, alt: str) -> dict[str, Any]:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        r = self.client.post(
            f"{self.base}/wp-json/wp/v2/media",
            auth=self._basic(),
            headers={"Content-Disposition": f'attachment; filename="{path.name}"', "Content-Type": mime},
            content=path.read_bytes(),
            timeout=120,
        )
        r.raise_for_status()
        media = r.json()
        self.client.post(f"{self.base}/wp-json/wp/v2/media/{media['id']}", auth=self._basic(), json={"alt_text": alt})
        return media

    def _publish_core(self, article: dict[str, Any], status: str) -> PublishResult:
        auth = self._basic()
        existing = self.client.get(f"{self.base}/wp-json/wp/v2/posts", params={"slug": article["slug"], "status": "any"}, auth=auth)
        if existing.status_code == 200 and existing.json():
            post = existing.json()[0]
            return PublishResult(str(post["id"]), post.get("link", ""), post.get("status", ""), duplicate=True)
        body: dict[str, Any] = {
            "title": article["h1"], "content": article["html"], "status": status, "slug": article["slug"],
            "excerpt": article.get("excerpt") or "",
            # Only applied if the site registers these keys with show_in_rest (Yoast/Rank Math don't by default)
            "meta": {"_yoast_wpseo_metadesc": article["meta_description"], "_yoast_wpseo_title": article["meta_title"]},
        }
        image_url = ""
        if article.get("image_path"):
            media = self.upload_media(Path(article["image_path"]), article.get("image_alt") or article["h1"])
            body["featured_media"], image_url = media["id"], media.get("source_url", "")
        r = self.client.post(f"{self.base}/wp-json/wp/v2/posts", auth=auth, json=body, timeout=120)
        if r.status_code == 400 and "meta" in r.text:
            body.pop("meta")
            r = self.client.post(f"{self.base}/wp-json/wp/v2/posts", auth=auth, json=body, timeout=120)
        r.raise_for_status()
        post = r.json()
        return PublishResult(str(post["id"]), post.get("link", ""), post.get("status", status), image_url=image_url)
