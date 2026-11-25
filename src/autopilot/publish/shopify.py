"""Shopify Admin GraphQL `articleCreate` (custom app token with write_content scope)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import Config, secret
from . import PublishResult, RemoteDoc, slug_from_url

FIND = """query($q: String!) { articles(first: 1, query: $q) { nodes { id handle isPublished } } }"""

CREATE = """mutation articleCreate($article: ArticleCreateInput!) {
  articleCreate(article: $article) { article { id handle isPublished } userErrors { field message } }
}"""

GET = """query($q: String!) { articles(first: 1, query: $q) { nodes {
  id handle title body summary updatedAt
  seoTitle: metafield(namespace: "global", key: "title_tag") { value }
  seoDescription: metafield(namespace: "global", key: "description_tag") { value }
} } }"""

UPDATE = """mutation articleUpdate($id: ID!, $article: ArticleUpdateInput!) {
  articleUpdate(id: $id, article: $article) { article { id handle isPublished } userErrors { field message } }
}"""


class ShopifyPublisher:
    def __init__(self, cfg: Config, client: httpx.Client):
        self.sc = cfg.publish.shopify
        self.client = client
        self.endpoint = f"https://{self.sc.shop}/admin/api/{self.sc.api_version}/graphql.json"

    def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(self.endpoint, headers={"X-Shopify-Access-Token": secret(self.sc.token_env)},
                             json={"query": query, "variables": variables})
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"Shopify GraphQL error: {json.dumps(data['errors'])[:300]}")
        return data["data"]

    def url_for(self, handle: str) -> str:
        return f"{self.sc.blog_url.rstrip('/')}/{handle}" if self.sc.blog_url else ""

    def publish(self, article: dict[str, Any], status: str) -> PublishResult:
        found = self._gql(FIND, {"q": f"handle:{article['slug']}"})["articles"]["nodes"]
        if found:
            a = found[0]
            return PublishResult(a["id"], self.url_for(a["handle"]), "publish" if a["isPublished"] else "draft", duplicate=True)
        schema = "".join(f'<script type="application/ld+json">{json.dumps(s)}</script>' for s in json.loads(article.get("schema_json") or "[]"))
        payload: dict[str, Any] = {
            "blogId": self.sc.blog_id,
            "title": article["h1"],
            "handle": article["slug"],
            "body": article["html"] + schema,
            "summary": article.get("excerpt") or "",
            "isPublished": status == "publish",
            "author": {"name": self.sc.author or "Editorial"},
            "metafields": [
                {"namespace": "global", "key": "title_tag", "type": "single_line_text_field", "value": article["meta_title"]},
                {"namespace": "global", "key": "description_tag", "type": "single_line_text_field", "value": article["meta_description"]},
            ],
        }
        if article.get("image_url"):
            payload["image"] = {"url": article["image_url"], "altText": article.get("image_alt") or article["h1"]}
        res = self._gql(CREATE, {"article": payload})["articleCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"Shopify articleCreate: {res['userErrors']}")
        a = res["article"]
        return PublishResult(a["id"], self.url_for(a["handle"]), "publish" if a["isPublished"] else "draft")

    # -- refresh an existing article ---------------------------------------
    def fetch(self, url: str) -> RemoteDoc:
        handle = slug_from_url(url)
        found = self._gql(GET, {"q": f"handle:{handle}"})["articles"]["nodes"]
        if not found:
            raise LookupError(f"No Shopify article with handle '{handle}'")
        a = found[0]
        return RemoteDoc(
            remote_id=a["id"], url=self.url_for(a["handle"]) or url,
            title=(a.get("seoTitle") or {}).get("value") or a.get("title", ""),
            description=(a.get("seoDescription") or {}).get("value") or a.get("summary") or "",
            body=a.get("body") or "", format="html", version=a.get("updatedAt", ""), h1=a.get("title", ""),
        )

    def update(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult:
        payload = {
            "body": body,
            "metafields": [
                {"namespace": "global", "key": "title_tag", "type": "single_line_text_field", "value": title},
                {"namespace": "global", "key": "description_tag", "type": "single_line_text_field", "value": description},
            ],
        }
        res = self._gql(UPDATE, {"id": doc.remote_id, "article": payload})["articleUpdate"]
        if res["userErrors"]:
            raise RuntimeError(f"Shopify articleUpdate: {res['userErrors']}")
        a = res["article"]
        return PublishResult(a["id"], self.url_for(a["handle"]) or doc.url, "publish" if a["isPublished"] else "draft")
