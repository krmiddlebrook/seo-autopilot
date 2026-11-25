"""Crawling sites that make it hard: JS-rendered SPAs, broken sitemaps, custom money-page paths."""

import httpx

from autopilot.crawl import classify, crawl_site, find_sitemap, homepage_health
from autopilot.http import make_client

SPA = "https://spa.test"
SHELL = "<html><head><title>App</title></head><body><div id='root'></div><script src='/app.js'></script></body></html>"
FILLER = " ".join(["Plain server-rendered text about training plans, recovery and coaching for real people."] * 10)


def article(title: str, links: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{FILLER}</p>{links}</main></body></html>"


def spa_site(request: httpx.Request) -> httpx.Response:
    """Sitemap errors, the homepage and unknown paths serve the same JS shell, but /blog is server-rendered."""
    path = request.url.path
    html = {"content-type": "text/html"}
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\n")
    if path == "/sitemap.xml":
        return httpx.Response(500, text="error")
    if path == "/sitemap_index.xml":
        return httpx.Response(200, text=SHELL, headers=html)  # SPA fallback route instead of XML
    if path == "/blog":
        return httpx.Response(200, text=article("Blog", "<a href='/blog/first-post'>1</a><a href='/blog/second-post'>2</a>"
                                                       "<a href='/challenge'>Join</a>"), headers=html)
    if path in ("/blog/first-post", "/blog/second-post", "/challenge"):
        return httpx.Response(200, text=article(path.rsplit("/", 1)[-1]), headers=html)
    return httpx.Response(200, text=SHELL, headers=html)


def test_spa_with_broken_sitemap_is_crawled_through_hub_paths():
    client = make_client(transport=httpx.MockTransport(spa_site))
    assert find_sitemap(client, SPA) == ("", [])
    pages = crawl_site(client, SPA, money_pages=["^/challenge"])
    urls = {p["url"] for p in pages}
    assert {f"{SPA}/blog", f"{SPA}/blog/first-post", f"{SPA}/blog/second-post", f"{SPA}/challenge"} <= urls
    assert len([p for p in pages if p["title"] == "App"]) == 1  # the shell is kept once, not once per path
    assert {p["url"]: p["kind"] for p in pages}[f"{SPA}/challenge"] == "money"
    assert homepage_health(client, SPA)[1] == 0


def test_seed_urls_reach_sections_outside_the_hub_list():
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/journal":
            return httpx.Response(200, text=article("Journal", "<a href='/journal/entry'>e</a>"), headers={"content-type": "text/html"})
        if request.url.path == "/journal/entry":
            return httpx.Response(200, text=article("Entry"), headers={"content-type": "text/html"})
        return spa_site(request)

    client = make_client(transport=httpx.MockTransport(site))
    urls = {p["url"] for p in crawl_site(client, SPA, seeds=["/journal"])}
    assert f"{SPA}/journal/entry" in urls


def test_classify_custom_money_pages():
    assert classify(f"{SPA}/pricing") == "money"
    assert classify(f"{SPA}/challenge") == "page"
    assert classify(f"{SPA}/challenge", ["^/challenge"]) == "money"
    assert classify(f"{SPA}/blog/the-challenge-of-x", ["^/challenge"]) != "money"


def test_doctor_warns_about_spa_homepage_and_missing_sitemap(tmp_path, capsys, monkeypatch):
    from autopilot import cli

    monkeypatch.chdir(tmp_path)
    cli.main(["init", "--url", SPA, "--name", "Spa"])
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: make_client(transport=httpx.MockTransport(spa_site)))
    c = cli.Ctx(cli.build_parser().parse_args(["--fake", "doctor"]))
    warnings = []
    cli.site_checks(c, c.cfg, lambda name, detail: warnings.append(name))
    assert warnings == ["sitemap", "homepage HTML"]
