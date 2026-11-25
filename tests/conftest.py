import json
from pathlib import Path

import httpx
import pytest

from autopilot.config import CONFIG_TEMPLATE, load_config
from autopilot.db import DB

SITE = "https://example.com"

PAGES = {
    "/": ("Example Co", "<h1>Example Co</h1><p>We help busy homeowners with home maintenance and budget planning.</p>"),
    "/services/gutter-cleaning/": ("Gutter Cleaning Service", "<h1>Gutter cleaning</h1><p>Professional gutter cleaning "
                                   "for homeowners from $149 per visit. Book a gutter cleaning visit and protect your roof and home maintenance budget.</p>"),
    "/blog/fall-home-maintenance-checklist/": ("Fall Home Maintenance Checklist", "<h1>Fall checklist</h1><p>A seasonal "
                                               "home maintenance checklist covering gutters, heating and windows.</p>"),
    "/blog/budget-planning-for-repairs/": ("Budget Planning for Home Repairs", "<h1>Budget planning</h1><p>How to plan "
                                           "a budget for home repairs and maintenance costs.</p>"),
}


def page_html(title: str, body: str) -> str:
    filler = " ".join(["Homeowners ask us about maintenance, costs, timing and choosing the right approach."] * 12)
    return (f"<html><head><title>{title}</title><meta name='description' content='{title} from Example Co'></head>"
            f"<body><main>{body}<p>{filler}</p></main></body></html>")


class FakeWeb:
    """MockTransport handler for the site, autocomplete, IndexNow and a WordPress receiver."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.posts: dict[str, dict] = {}
        self.updates = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "suggestqueries.google.com":
            q = url.params["q"]
            return httpx.Response(200, json=[q, [f"{q} cost", f"{q} tips for beginners", f"{q} near me"]])
        if url.host == "api.indexnow.org":
            return httpx.Response(202)
        if url.host == "example.com":
            path = url.path
            if path == "/robots.txt":
                return httpx.Response(200, text=f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
            if path == "/sitemap.xml":
                locs = "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in PAGES)
                return httpx.Response(200, text=f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>',
                                      headers={"content-type": "application/xml"})
            if path in PAGES:
                return httpx.Response(200, text=page_html(*PAGES[path]), headers={"content-type": "text/html"})
            if path == "/wp-json/autopilot/v1/publish":
                return self.wp_publish(request)
            if path == "/wp-json/autopilot/v1/content":
                return self.wp_content(request)
            if path == "/wp-json/autopilot/v1/update":
                return self.wp_update(request)
            if path == "/wp-json/autopilot/v1/setup-indexnow":
                return httpx.Response(200, json={"success": True, "key": "abc123", "key_location": f"{SITE}/abc123.txt"})
        return httpx.Response(404)

    def wp_publish(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer test-key":
            return httpx.Response(401, json={"code": "autopilot_unauthorized"})
        body = json.loads(request.content)
        uid = body["soro_article_id"]
        if uid in self.posts:
            return httpx.Response(200, json={"success": True, "post": self.posts[uid], "duplicate": True})
        post = {"post_id": len(self.posts) + 100, "url": f"{SITE}/{body['slug']}/", "status": body["status"]}
        self.posts[uid] = {**post, "body": body, "modified": "2026-01-01T00:00:00"}
        return httpx.Response(200, json={"success": True, "post": post, "duplicate": False, "slug_conflict": False})

    def _post_json(self, p: dict) -> dict:
        b = p["body"]
        return {"post_id": p["post_id"], "url": p["url"], "title": b["title"], "meta_title": b.get("meta_title", ""),
                "meta_description": b.get("meta_description", ""), "content": b["content"], "modified": p["modified"],
                "status": p["status"]}

    def _find_post(self, key: str, value) -> dict | None:
        return next((p for p in self.posts.values() if str(p[key]).rstrip("/") == str(value).rstrip("/")), None)

    def wp_content(self, request: httpx.Request) -> httpx.Response:
        p = self._find_post("url", request.url.params.get("url", ""))
        return httpx.Response(200, json=self._post_json(p)) if p else httpx.Response(404, json={"code": "not_found"})

    def wp_update(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        p = self._find_post("post_id", data["post_id"])
        if not p:
            return httpx.Response(404, json={"code": "not_found"})
        if data.get("expected_modified") and data["expected_modified"] != p["modified"]:
            return httpx.Response(409, json={"code": "autopilot_conflict"})
        p["body"] = {**p["body"], "content": data["content"], "meta_title": data["meta_title"],
                     "meta_description": data["meta_description"]}
        p["modified"] = "2026-02-01T00:00:00"
        self.updates += 1
        return httpx.Response(200, json={"success": True, "post": self._post_json(p)})


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture
def client(web):
    from autopilot.http import make_client

    return make_client(transport=httpx.MockTransport(web))


@pytest.fixture
def cfg(tmp_path: Path):
    path = tmp_path / "autopilot.toml"
    path.write_text(CONFIG_TEMPLATE.format(url=SITE, name="Example Co", host="example.com"))
    c = load_config(path)
    c.llm.provider = "fake"
    c.search.provider = "none"
    c.writing.min_words = 900
    # no search provider means no competitor pages, so the unique-value check can't confirm anything and would hold
    # every draft; tests that exercise it turn it back on
    c.writing.unique_value = False
    return c


@pytest.fixture
def db(cfg):
    d = DB(cfg.db_path)
    yield d
    d.close()

