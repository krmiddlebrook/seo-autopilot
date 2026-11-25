import json

import httpx

from autopilot.http import make_client
from autopilot.publish.markdown import MarkdownPublisher
from autopilot.publish.webhook import WebhookPublisher
from autopilot.publish.wordpress import WordPressPublisher

ARTICLE = {
    "uid": "u" * 32, "keyword": "gutter cleaning cost", "slug": "gutter-cleaning-cost", "h1": "Gutter Cleaning Cost",
    "meta_title": "What Gutter Cleaning Costs in 2026", "meta_description": "Prices by home size.", "excerpt": "Prices.",
    "markdown": "Body text.\n", "html": "<p>Body text.</p>", "schema_json": json.dumps([{"@type": "BlogPosting"}]),
    "image_url": "", "image_path": "", "image_alt": "", "image_credit": "", "score": 90, "remote_url": "",
}


def test_wordpress_payload_and_idempotency(cfg, client, web, monkeypatch):
    monkeypatch.setenv("WP_RECEIVER_KEY", "test-key")
    wp = WordPressPublisher(cfg, client)
    payload = wp.payload(ARTICLE, "draft")
    assert payload["soro_article_id"] == ARTICLE["uid"]
    assert payload["meta_title"] == ARTICLE["meta_title"] and payload["schema"] == [{"@type": "BlogPosting"}]
    first = wp.publish(ARTICLE, "publish")
    second = wp.publish(ARTICLE, "publish")
    assert first.url == "https://example.com/gutter-cleaning-cost/" and not first.duplicate
    assert second.duplicate and second.remote_id == first.remote_id
    assert len(web.posts) == 1


def test_soro_namespace_omits_unsupported_fields(cfg, client):
    cfg.publish.wordpress.namespace = "soro/v1"
    payload = WordPressPublisher(cfg, client).payload(ARTICLE, "draft")
    assert "meta_title" not in payload and "schema" not in payload


def test_markdown_publisher_idempotent_and_slug_guard(cfg, tmp_path):
    cfg.publish.markdown.dir = str(tmp_path / "content")
    pub = MarkdownPublisher(cfg)
    res = pub.publish(ARTICLE, "publish")
    text = (tmp_path / "content" / "gutter-cleaning-cost.mdx").read_text()
    assert text.startswith("---\n") and 'seoTitle: "What Gutter Cleaning Costs in 2026"' in text
    assert "draft: false" in text
    assert res.url == "https://example.com/blog/gutter-cleaning-cost"
    assert pub.publish(ARTICLE, "publish").duplicate
    other = pub.publish({**ARTICLE, "uid": "v" * 32}, "publish")
    assert other.slug_conflict and other.status == "draft"


def test_markdown_json_frontmatter_with_renamed_fields(cfg, tmp_path):
    img = tmp_path / "hero.png"
    img.write_bytes(b"png")
    m = cfg.publish.markdown
    m.dir, m.format, m.public_dir, m.image_url_prefix = str(tmp_path / "blog"), "md", str(tmp_path / "blog"), "media"
    m.frontmatter, m.image_ref = "json", "filename"
    m.fields = {"description": "meta_description", "image": "hero_image"}
    m.defaults = {"cta_label": "Take the challenge", "hero_image": "fallback.png"}
    MarkdownPublisher(cfg).publish({**ARTICLE, "image_path": str(img)}, "publish")
    head, body = (tmp_path / "blog" / "gutter-cleaning-cost.md").read_text().split("\n---\n", 1)
    meta = json.loads(head.removeprefix("---\n"))
    assert meta["meta_description"] == ARTICLE["meta_description"] and "description" not in meta
    assert meta["hero_image"] == "hero.png" and (tmp_path / "blog" / "media" / "hero.png").exists()
    assert meta["cta_label"] == "Take the challenge"
    assert body.strip() == ARTICLE["markdown"].strip()


def test_webhook_signature(cfg, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sig"] = request.headers["x-autopilot-signature"]
        seen["body"] = request.content
        return httpx.Response(200, json={"id": 7, "url": "https://example.com/p"})

    import hashlib
    import hmac

    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    cfg.publish.webhook.url = "https://hooks.example.net/in"
    cfg.publish.webhook.secret_env = "HOOK_SECRET"
    res = WebhookPublisher(cfg, make_client(transport=httpx.MockTransport(handler))).publish(ARTICLE, "publish")
    assert res.remote_id == "7"
    assert seen["sig"] == "sha256=" + hmac.new(b"s3cret", seen["body"], hashlib.sha256).hexdigest()


def test_markdown_update_existing_post(cfg, tmp_path):
    cfg.publish.markdown.dir = str(tmp_path / "content")
    pub = MarkdownPublisher(cfg)
    res = pub.publish(ARTICLE, "publish")
    doc = pub.fetch(res.url)
    assert doc.title == "What Gutter Cleaning Costs in 2026" and doc.body.startswith("Body text.")
    pub.update(doc, "Gutter Cleaning Cost by Home Size", "New description.", doc.body + "\n## New section\n\nMore.\n")
    text = (tmp_path / "content" / "gutter-cleaning-cost.mdx").read_text()
    assert 'seoTitle: "Gutter Cleaning Cost by Home Size"' in text and "## New section" in text
    assert "New description." in text


def test_ghost_update_sends_updated_at(cfg, monkeypatch):
    from autopilot.publish.ghost import GhostPublisher

    monkeypatch.setenv("GHOST_ADMIN_KEY", "abc:" + "00" * 32)
    cfg.publish.ghost.url = "https://ghost.example.com"
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"posts": [{"id": "p1", "url": "https://ghost.example.com/x/", "title": "X",
                                                        "meta_title": "X title", "html": "<p>a</p>",
                                                        "updated_at": "2026-01-01T00:00:00.000Z"}]})
        sent.append(json.loads(request.content)["posts"][0])
        return httpx.Response(200, json={"posts": [{"id": "p1", "url": "https://ghost.example.com/x/", "status": "published"}]})

    pub = GhostPublisher(cfg, make_client(transport=httpx.MockTransport(handler)))
    doc = pub.fetch("https://ghost.example.com/x/")
    pub.update(doc, "New title", "New desc", "<p>b</p>")
    assert sent[0] == {"html": "<p>b</p>", "meta_title": "New title", "meta_description": "New desc",
                       "updated_at": "2026-01-01T00:00:00.000Z"}


def test_shopify_update_article(cfg, monkeypatch):
    from autopilot.publish.shopify import ShopifyPublisher

    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "t")
    cfg.publish.shopify.shop, cfg.publish.shopify.blog_url = "s.myshopify.com", "https://s.com/blogs/news"
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "articleUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"articleUpdate": {"article": {"id": "gid://a/1", "handle": "x",
                                                                                     "isPublished": True}, "userErrors": []}}})
        return httpx.Response(200, json={"data": {"articles": {"nodes": [{
            "id": "gid://a/1", "handle": "x", "title": "X", "body": "<p>a</p>", "summary": "", "updatedAt": "t",
            "seoTitle": {"value": "X title"}, "seoDescription": None}]}}})

    pub = ShopifyPublisher(cfg, make_client(transport=httpx.MockTransport(handler)))
    doc = pub.fetch("https://s.com/blogs/news/x")
    assert doc.title == "X title"
    res = pub.update(doc, "New", "Desc", "<p>b</p>")
    assert res.url == "https://s.com/blogs/news/x" and calls[-1]["variables"]["article"]["body"] == "<p>b</p>"
