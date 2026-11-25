import httpx

from autopilot import health, pipeline
from autopilot.http import make_client
from autopilot.llm import make_llm

from conftest import SITE, page_html

LINKS = "".join(f"<a href='{p}'>x</a>" for p in ("/", "/services/gutter-cleaning/", "/blog/a/", "/blog/b/"))


def html(title: str, body: str = "", head: str = "", links: bool = True) -> str:
    page = page_html(title, f"<h1>{title}</h1>{body}" + (LINKS if links else ""))
    return page.replace("</head>", head + "</head>")


SHELL = "<html><head><title>App</title></head><body><div id='root'></div><script src='/app.js'></script></body></html>"


class BrokenSite:
    """A site with one of each problem the health check should catch."""

    def __init__(self, sitemap=True):
        self.sitemap = sitemap
        self.pages = {
            "/": (200, html("Home", head=f"<link rel='canonical' href='{SITE}/'>"), {}),
            "/services/gutter-cleaning/": (200, html("Gutters", head=f"<link rel='canonical' href='{SITE}/services/gutter-cleaning/'>"), {}),
            "/blog/a/": (200, html("A", head="<link rel='canonical' href='https://other.com/a/'>"), {}),
            "/blog/b/": (200, html("B", head=f"<link rel='canonical' href='{SITE}/blog/b/'><link rel='canonical' href='{SITE}/blog/c/'>"
                                   "<meta name='robots' content='noindex,follow'>"), {}),
            "/blog/orphan/": (200, html("Orphan", head=f"<link rel='canonical' href='{SITE}/blog/orphan/'>", links=False), {}),
            "/blog/header-noindex/": (200, html("Hdr"), {"x-robots-tag": "noindex"}),
            "/app/": (200, SHELL, {}),
            "/app2/": (200, SHELL, {}),
            "/gone/": (404, "", {}),
            "/moved/": (301, "", {"location": f"{SITE}/gone/"}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/sitemap.xml" and self.sitemap:
            locs = "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in self.pages)
            return httpx.Response(200, text=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>', headers={"content-type": "application/xml"})
        if path in self.pages:
            code, body, headers = self.pages[path]
            return httpx.Response(code, text=body, headers={"content-type": "text/html", **headers})
        return httpx.Response(404)


def run(cfg, site, **kw):
    return health.check_site(cfg, make_client(transport=httpx.MockTransport(site)), workers=1, **kw)


def found(rep, check, path, text=""):
    return [i for i in rep.issues if i.check == check and i.url == SITE + path and text in i.detail]


def test_flags_every_kind_of_problem(cfg):
    rep = run(cfg, BrokenSite())
    assert found(rep, "status", "/gone/", "HTTP 404")[0].severity == "error"
    assert found(rep, "redirect", "/moved/", "HTTP 404")[0].severity == "error"  # redirect to a dead page
    assert found(rep, "canonical", "/blog/a/", "another site")
    assert found(rep, "canonical", "/blog/b/", "2 different canonical")
    assert found(rep, "noindex", "/blog/b/", "meta robots")[0].severity == "error"
    assert found(rep, "noindex", "/blog/header-noindex/", "X-Robots-Tag")
    assert found(rep, "orphan", "/blog/orphan/")
    assert not found(rep, "orphan", "/blog/a/")
    assert found(rep, "content", "/app2/", "byte-identical")
    assert found(rep, "content", "/app/", "words of main text")
    assert rep.status == "errors" and not rep.ok
    # every step reports what it covered against what it expected
    steps = {s.name: s for s in rep.steps}
    assert steps["status codes"].expected == 10 and steps["status codes"].actual == 10
    assert all(s.ok for s in rep.steps)
    # the shell duplicate is not handed to the crawl as a page
    assert SITE + "/app2/" not in {p["url"] for p in rep.pages}


def test_missing_sources_are_listed_with_what_they_unlock(cfg):
    rep = run(cfg, BrokenSite())
    names = {s.name: s for s in rep.sources}
    assert "Google Search Console" in names and "indexed" in names["Google Search Console"].unlocks
    assert "previous health report" in names
    assert "Missing data sources" in rep.text()


def test_no_sitemap_is_an_error_and_a_missing_source(cfg):
    rep = run(cfg, BrokenSite(sitemap=False))
    steps = {s.name: s for s in rep.steps}
    assert not steps["sitemap"].ok and rep.status == "incomplete"
    assert any(s.name == "sitemap.xml" for s in rep.sources)
    # it still checks the homepage and what it links to
    assert steps["status codes"].expected >= 4


def test_truncated_sitemap_fails_its_step(cfg):
    cfg.health.max_urls = 3
    rep = run(cfg, BrokenSite())
    step = next(s for s in rep.steps if s.name == "sitemap")
    assert not step.ok and "health.max_urls" in step.detail
    assert rep.status == "incomplete"
    assert any(s.status == "partial" for s in rep.sources)


def test_gsc_zero_rows_or_failure_never_passes_quietly(cfg):
    cfg.topics.gsc_enabled = True
    rep = run(cfg, BrokenSite(), gsc_rows=lambda: [])
    assert not next(s for s in rep.steps if s.name == "Search Console").ok

    def boom():
        raise RuntimeError("token expired")

    rep = run(cfg, BrokenSite(), gsc_rows=boom)
    assert any(s.name == "Google Search Console" and s.status == "failed" for s in rep.sources)
    assert rep.status == "incomplete"

    rep = run(cfg, BrokenSite(), gsc_rows=lambda: [{"keys": [SITE + "/"]}])
    assert found(rep, "gsc", "/blog/a/", "no Google impressions")
    assert not found(rep, "gsc", "/")


def test_diff_against_previous_report(cfg):
    site = BrokenSite()
    first = run(cfg, site)
    site.pages["/gone/"] = (200, html("Back", head=f"<link rel='canonical' href='{SITE}/gone/'>"), {})
    site.pages["/blog/new-404/"] = (404, "", {})
    second = run(cfg, site, previous=first.to_dict())
    assert any("/gone/" in k for k in second.fixed_issues)
    assert any("/blog/new-404/" in k for k in second.new_issues)
    assert not any(s.name == "previous health report" for s in second.sources)


def test_healthy_fake_site(cfg, client):
    rep = health.check_site(cfg, client, workers=1)
    assert rep.status == "ok", rep.text()
    assert len(rep.pages) == 4


def test_daily_check_saves_history_and_refreshes_pages(cfg, db, client):
    rep = pipeline.daily_check(cfg, db, client, log=lambda m: None)
    assert db.get(health.LAST_KEY)["status"] == rep.status
    assert len(db.get(health.HISTORY_KEY)) == 1
    assert len(db.pages()) == 4
    assert not pipeline.health_due(db)


def test_run_once_records_a_crashed_health_check(cfg, db, client, monkeypatch):
    pipeline.crawl(cfg, db, client, log=lambda m: None)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(pipeline, "daily_check", boom)
    logs = []
    pipeline.run_once(cfg, db, client, make_llm(cfg.llm, client), publish=False, echo=logs.append)
    assert any("Health check failed" in m for m in logs)
    assert db.get(health.HISTORY_KEY)[-1]["status"] == "failed"
