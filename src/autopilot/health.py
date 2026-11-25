"""Daily site health check: status codes, canonicals, stray noindex, orphan pages and content missing from the
server HTML.

The rule is no silent false success. Every step records how many things it expected to look at and how many it
actually looked at, and a step whose counts don't add up fails even when it found no problems: "0 issues" from a check
that fetched 3 of 40 pages is not a clean bill of health. Every data source the check could not use is listed with
what it would unlock, so a short report never looks like a clean one.
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
from lxml import etree, html as lxml_html

from .config import Config
from .crawl import SKIP_PATTERNS, classify, discover_sitemaps, extract_page, sitemap_urls
from .db import DB, now

LAST_KEY = "health:last"
HISTORY_KEY = "health:history"
HISTORY_DAYS = 30


@dataclass
class Step:
    """One check and its own accounting: what it set out to cover, what it covered, and whether that adds up."""
    name: str
    expected: int
    actual: int
    ok: bool
    detail: str = ""


@dataclass
class Issue:
    severity: str  # error | warn
    check: str  # status | redirect | canonical | noindex | orphan | content | robots | sitemap | gsc
    url: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.check}|{self.url}|{self.detail}"


@dataclass
class Source:
    """A data source the check could not use, and what it would add."""
    name: str
    status: str  # missing | failed | partial
    unlocks: str
    fix: str = ""


@dataclass
class Fetched:
    url: str
    status: int  # 0 = the request failed
    location: str = ""  # redirect target
    final_status: int = 0  # status of the redirect target
    content_type: str = ""
    x_robots: str = ""
    body: str = ""
    error: str = ""


@dataclass
class HealthReport:
    site: str
    started_at: str = field(default_factory=now)
    finished_at: str = ""
    steps: list[Step] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    new_issues: list[str] = field(default_factory=list)  # issue keys not in the previous report
    fixed_issues: list[str] = field(default_factory=list)  # issue keys from the previous report that are gone
    pages: list[dict[str, Any]] = field(default_factory=list)  # crawl-ready pages built from the fetched HTML
    statuses: dict[str, int] = field(default_factory=dict)  # url -> HTTP status (0 = no response)

    def step(self, name: str, expected: int, actual: int, detail: str = "", ok: bool | None = None) -> Step:
        s = Step(name, expected, actual, actual >= expected if ok is None else ok, detail)
        self.steps.append(s)
        return s

    def issue(self, severity: str, check: str, url: str, detail: str) -> None:
        self.issues.append(Issue(severity, check, url, detail))

    def source(self, name: str, status: str, unlocks: str, fix: str = "") -> None:
        self.sources.append(Source(name, status, unlocks, fix))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.failed_steps

    @property
    def status(self) -> str:
        if self.failed_steps:
            return "incomplete"
        return "errors" if self.errors else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site, "started_at": self.started_at, "finished_at": self.finished_at, "status": self.status,
            "steps": [asdict(s) for s in self.steps], "issues": [asdict(i) for i in self.issues],
            "sources": [asdict(s) for s in self.sources], "new_issues": self.new_issues,
            "fixed_issues": self.fixed_issues,
        }

    def summary(self) -> str:
        warns = len(self.issues) - len(self.errors)
        return (f"Health {self.status}: {len(self.errors)} error(s), {warns} warning(s), "
                f"{len(self.failed_steps)} incomplete step(s), {len(self.sources)} missing data source(s)")

    def text(self) -> str:
        out = [f"Site health for {self.site} ({self.started_at})", self.summary(), "", "Steps (expected / covered):"]
        for s in self.steps:
            out.append(f"  {'✓' if s.ok else '✗'} {s.name}: {s.actual}/{s.expected} {s.detail}".rstrip())
        if self.issues:
            out += ["", "Issues:"]
            for i in sorted(self.issues, key=lambda i: (i.severity != "error", i.check, i.url)):
                new = " [new]" if i.key in self.new_issues else ""
                out.append(f"  {i.severity.upper():5} {i.check:9} {i.url}  {i.detail}{new}")
        if self.fixed_issues:
            out += ["", f"Fixed since last check: {len(self.fixed_issues)}"]
            out += [f"  {k.replace('|', '  ', 2)}" for k in self.fixed_issues[:20]]
        if self.sources:
            out += ["", "Missing data sources (what each would unlock):"]
            for s in self.sources:
                fix = f" Fix: {s.fix}" if s.fix else ""
                out.append(f"  - {s.name} ({s.status}): {s.unlocks}.{fix}")
        return "\n".join(out)


# -- helpers -----------------------------------------------------------------------------------------------------

def norm(url: str) -> str:
    """Compare URLs the way a crawler would: no fragment, lower-case host, no trailing slash, no default index."""
    p = urlparse(url.strip())
    path = re.sub(r"/index\.(?:html?|php)$", "/", p.path or "/").rstrip("/") or "/"
    query = f"?{p.query}" if p.query else ""
    return f"{p.scheme.lower()}://{p.netloc.lower().removeprefix('www.')}{path}{query}"


def same_site(url: str, site_url: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == urlparse(site_url).netloc.lower().removeprefix("www.")


def fetch(client: httpx.Client, url: str) -> Fetched:
    """One URL without following redirects, so the status the sitemap URL itself returns is what gets recorded."""
    try:
        r = client.get(url, follow_redirects=False)
    except httpx.HTTPError as exc:
        return Fetched(url, 0, error=f"{type(exc).__name__}: {exc}")
    f = Fetched(url, r.status_code, content_type=r.headers.get("content-type", ""),
                x_robots=", ".join(r.headers.get_list("x-robots-tag")))
    if r.is_redirect:
        f.location = urljoin(url, r.headers.get("location", ""))
        try:
            f.final_status = client.get(f.location).status_code if f.location else 0
        except httpx.HTTPError:
            f.final_status = 0
    elif r.status_code == 200 and "html" in (f.content_type or "html"):
        f.body = r.text
    return f


def parse(body: str) -> Any:
    try:
        return lxml_html.fromstring(body) if body.strip() else None
    except (etree.ParserError, ValueError):
        return None


def canonicals(doc: Any, base: str) -> list[str]:
    hrefs = doc.xpath("//link[translate(@rel,'CANONICAL','canonical')='canonical']/@href")
    return list(dict.fromkeys(urljoin(base, h.strip()) for h in hrefs if h.strip()))


def robots_directives(doc: Any) -> str:
    return ", ".join(doc.xpath("//meta[translate(@name,'ROBOTSGOOGLEBOT','robotsgooglebot')='robots' or "
                               "translate(@name,'ROBOTSGOOGLEBOT','robotsgooglebot')='googlebot']/@content"))


def internal_links(doc: Any, base: str, site_url: str, raw: bool = False) -> set[str]:
    """Same-site link targets, normalized for comparing; `raw` keeps them as written (for fetching)."""
    out = set()
    for href in doc.xpath("//a/@href"):
        href = href.strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        target = urljoin(base, href)
        if same_site(target, site_url):
            target = target.split("#")[0]
            out.add(target if raw else norm(target))
    return out


def blocks_everything(robots: str) -> bool:
    """robots.txt says `Disallow: /` to every crawler."""
    group, blocked = False, False
    for line in robots.splitlines():
        line = line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = (x.strip() for x in line.partition(":"))
        key = key.lower()
        if key == "user-agent":
            group = value == "*"
        elif group and key == "disallow" and value == "/":
            blocked = True
        elif group and key == "allow" and value == "/":
            blocked = False
    return blocked


# -- the check ---------------------------------------------------------------------------------------------------

def check_site(cfg: Config, client: httpx.Client, previous: dict[str, Any] | None = None,
               gsc_rows: Callable[[], list[dict[str, Any]]] | None = None, workers: int = 8) -> HealthReport:
    site = cfg.site.url.rstrip("/") + "/"
    hc = cfg.health
    rep = HealthReport(site=cfg.site.url)

    # 1. robots.txt
    robots_url = urljoin(site, "/robots.txt")
    try:
        r = client.get(robots_url)
        code, body = r.status_code, r.text
    except httpx.HTTPError as exc:
        code, body = 0, ""
        rep.step("robots.txt", 1, 0, f"no response: {exc}")
    if code == 200:
        rep.step("robots.txt", 1, 1, "HTTP 200")
        if blocks_everything(body):
            rep.issue("error", "robots", robots_url, "robots.txt disallows the whole site for every crawler")
    elif code:
        rep.step("robots.txt", 1, 1, f"HTTP {code}")
        rep.issue("warn", "robots", robots_url, f"robots.txt returned HTTP {code}")

    # 2. sitemap: which URLs the site says it wants indexed
    limit = hc.max_urls
    sitemap, entries, tried = "", [], []
    for sm in [cfg.site.sitemap_url] if cfg.site.sitemap_url else discover_sitemaps(client, site):
        tried.append(sm)
        entries = sitemap_urls(client, sm, limit=limit + 1)
        if entries:
            sitemap = sm
            break
    truncated = len(entries) > limit
    entries = entries[:limit]
    listed = list(dict.fromkeys(u for u, _ in entries))
    if sitemap:
        dupes = len(entries) - len(listed)
        detail = f"{len(listed)} URLs in {sitemap}" + (f", {dupes} listed twice" if dupes else "")
        if truncated:
            detail += f"; stopped at health.max_urls = {limit}, so the rest of the sitemap was not checked"
        rep.step("sitemap", 1, 1, detail, ok=not truncated)
        offsite = [u for u in listed if not same_site(u, site)]
        for u in offsite:
            rep.issue("warn", "sitemap", u, "sitemap lists a URL on another host")
        urls = [u for u in listed if same_site(u, site)]
    else:
        rep.step("sitemap", 1, 0, f"no sitemap with any URLs (tried {', '.join(tried) or 'nothing'})")
        rep.issue("error", "sitemap", sitemap or site, "no working sitemap.xml: search engines must find every page "
                                                    "by following links")
        rep.source("sitemap.xml", "missing", "the list of URLs you want indexed, so status codes, stray noindex and "
                   "orphan pages can be checked against it", "publish a sitemap.xml or set site.sitemap_url")
        urls = [site]  # check what we can: the homepage and what it links to
    if truncated:
        rep.source("full sitemap", "partial", f"checks for the URLs past the first {limit}",
                   "raise health.max_urls")

    # 3. status codes
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = dict(zip(urls, pool.map(lambda u: fetch(client, u), urls)))
    if not sitemap:  # no sitemap: follow the homepage's links one level so the other checks have something to see
        home = fetched.get(site)
        doc = parse(home.body) if home and home.body else None
        extra = sorted(u for u in internal_links(doc, site, site, raw=True) if not SKIP_PATTERNS.search(u))[:limit] if doc is not None else []
        extra = [u for u in extra if norm(u) not in {norm(x) for x in fetched}]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched.update(zip(extra, pool.map(lambda u: fetch(client, u), extra)))
    rep.statuses = {u: f.status for u, f in fetched.items()}
    answered = [f for f in fetched.values() if f.status]
    rep.step("status codes", len(fetched), len(answered),
             f"{sum(f.status == 200 for f in answered)} returned 200" +
             (f"; {len(fetched) - len(answered)} got no response" if len(answered) < len(fetched) else ""))
    for f in fetched.values():
        if not f.status:
            rep.issue("error", "status", f.url, f"no response: {f.error}")
        elif f.status >= 400:
            rep.issue("error", "status", f.url, f"HTTP {f.status}" + (" (listed in the sitemap)" if sitemap else ""))
        elif 300 <= f.status < 400:
            final = f" → HTTP {f.final_status}" if f.final_status != 200 else ""
            sev = "error" if f.final_status != 200 else "warn"
            rep.issue(sev, "redirect", f.url, f"HTTP {f.status} to {f.location}{final}" +
                      ("; list the final URL in the sitemap instead" if sitemap else ""))
        elif f.status == 200 and not f.body:
            rep.issue("warn", "status", f.url, f"200 but not HTML ({f.content_type or 'no content-type'})")

    ok_pages = {u: f for u, f in fetched.items() if f.status == 200 and f.body}
    docs = {u: parse(f.body) for u, f in ok_pages.items()}
    parsed = {u: d for u, d in docs.items() if d is not None}
    status_of = {norm(u): f.status for u, f in fetched.items()}

    # 4. canonicals
    extra_targets: dict[str, list[str]] = {}
    checked = 0
    for u, doc in parsed.items():
        canon = canonicals(doc, u)
        checked += 1
        if not canon:
            rep.issue("warn", "canonical", u, "no canonical tag")
            continue
        if len(canon) > 1:
            rep.issue("error", "canonical", u, f"{len(canon)} different canonical tags: {', '.join(canon)}")
            continue
        target = canon[0]
        if not same_site(target, site):
            rep.issue("error", "canonical", u, f"canonical points to another site: {target}")
        elif norm(target) != norm(u):
            rep.issue("warn", "canonical", u, f"canonical points to a different URL: {target}")
            if norm(target) not in status_of:
                extra_targets.setdefault(target, []).append(u)
            elif status_of[norm(target)] != 200:
                rep.issue("error", "canonical", u, f"canonical target {target} returns HTTP {status_of[norm(target)]}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for target, res in zip(extra_targets, pool.map(lambda t: fetch(client, t), extra_targets)):
            if res.status != 200:
                for u in extra_targets[target]:
                    rep.issue("error", "canonical", u, f"canonical target {target} returns HTTP {res.status or res.error}")
    rep.step("canonicals", len(ok_pages), checked, f"{len(extra_targets)} off-sitemap canonical target(s) fetched")

    # 5. stray noindex
    noindexed = 0
    for u, f in ok_pages.items():
        doc = docs.get(u)
        meta = robots_directives(doc) if doc is not None else ""
        where = [w for w, v in (("meta robots", meta), ("X-Robots-Tag header", f.x_robots)) if "noindex" in v.lower()]
        if where:
            noindexed += 1
            listed_note = " but the sitemap asks for it to be indexed" if sitemap else ""
            rep.issue("error" if sitemap or u == site else "warn", "noindex", u,
                      f"noindex in {' and '.join(where)}{listed_note}")
    rep.step("noindex", len(ok_pages), len(ok_pages), f"{noindexed} page(s) carry noindex")

    # 6. orphan pages: listed in the sitemap, linked from no other page we fetched
    inbound: dict[str, set[str]] = {}
    for u, doc in parsed.items():
        for target in internal_links(doc, u, site):
            if target != norm(u):
                inbound.setdefault(target, set()).add(u)
    if sitemap:
        candidates = [u for u in ok_pages if norm(u) != norm(site) and not SKIP_PATTERNS.search(u)]
        orphans = [u for u in candidates if not inbound.get(norm(u))]
        for u in orphans:
            rep.issue("warn", "orphan", u, "no internal links point here from any other checked page")
        partial = truncated or len(parsed) < len(ok_pages)
        rep.step("orphan pages", len(ok_pages), len(parsed),
                 f"links read from {len(parsed)} page(s); {len(orphans)} orphan(s)" +
                 ("; partial: some pages were not checked, so orphans may be linked from them" if partial else ""),
                 ok=not partial and len(parsed) == len(ok_pages))
    else:
        rep.step("orphan pages", 1, 0, "skipped: orphans are sitemap URLs nothing links to, and there is no sitemap")
    broken = sorted({t for t in inbound if status_of.get(t, 200) >= 400})
    for t in broken:
        rep.issue("error", "status", t, f"HTTP {status_of[t]}, linked from {len(inbound[t])} page(s), "
                                        f"e.g. {sorted(inbound[t])[0]}")

    # 7. content missing from the server HTML (what a crawler sees before JavaScript runs)
    seen: dict[bytes, str] = {}
    measured = 0
    for u, f in ok_pages.items():
        page = extract_page(u, f.body, cfg.site.money_pages)
        measured += 1
        words = len(page["text"].split())
        digest = hashlib.sha1(f.body.encode()).digest()
        kind = classify(u, cfg.site.money_pages)
        if digest in seen:
            rep.issue("error", "content", u, f"serves byte-identical HTML to {seen[digest]} (an app shell or a "
                                             "duplicate)")
        else:
            seen[digest] = u
            page["lastmod"] = next((m for x, m in entries if x == u), "")
            if not SKIP_PATTERNS.search(u):
                rep.pages.append(page)
        if words < hc.min_words and kind != "utility":
            sev = "error" if kind in ("home", "money", "post") else "warn"
            rep.issue(sev, "content", u, f"only {words} words of main text in the server HTML (want {hc.min_words}+); "
                                         "if the page renders with JavaScript, crawlers and AI assistants may not see it")
        doc = docs.get(u)
        if doc is not None and not doc.xpath("//h1"):
            rep.issue("warn", "content", u, "no <h1> in the server HTML")
        if not page["title"]:
            rep.issue("warn", "content", u, "no <title>")
    rep.step("server HTML content", len(ok_pages), measured, f"{len(rep.pages)} unique page(s) extracted")

    # 8. Google's own view, when Search Console is connected
    if not cfg.topics.gsc_enabled:
        rep.source("Google Search Console", "missing",
                   "which pages Google actually indexed, clicks and impressions per page, and sitemap pages Google "
                   "never shows", "set topics.gsc_enabled = true (see `autopilot doctor`)")
    elif gsc_rows is not None:
        try:
            rows = gsc_rows()
        except Exception as exc:  # any auth or API failure: say so, never skip quietly
            rep.step("Search Console", 1, 0, f"query failed: {exc}")
            rep.source("Google Search Console", "failed", "indexed pages, clicks and impressions per page", str(exc))
        else:
            seen_pages = {norm(r["keys"][0]) for r in rows if r.get("keys")}
            rep.step("Search Console", 1, 1 if rows else 0,
                     f"{len(rows)} page row(s) returned" + ("" if rows else "; zero rows usually means gsc_site does "
                                                             "not match the property"), ok=bool(rows))
            if rows:
                unseen = [u for u in ok_pages if norm(u) not in seen_pages]
                for u in unseen:
                    rep.issue("warn", "gsc", u, f"no Google impressions in the last {cfg.health.gsc_days} days")
    if cfg.topics.gsc_enabled:
        rep.source("URL Inspection API", "missing", "per-URL index status and the canonical Google picked",
                   "not built yet: search analytics only shows pages that earned impressions")

    # 9. compare with the last report
    if previous and previous.get("issues"):
        before = {f"{i['check']}|{i['url']}|{i['detail']}" for i in previous["issues"]}
        now_keys = {i.key for i in rep.issues}
        rep.new_issues = sorted(now_keys - before)
        rep.fixed_issues = sorted(before - now_keys)
    elif not previous:
        rep.source("previous health report", "missing", "what broke since the last check (new 404s, lost canonicals, "
                   "new noindex)", "it fills in after the next daily run")
    else:
        rep.new_issues = sorted(i.key for i in rep.issues)
    rep.finished_at = now()
    return rep


def save(db: DB, rep: HealthReport) -> None:
    data = rep.to_dict()
    data["summary"], data["text"] = rep.summary(), rep.text()
    db.set(LAST_KEY, data)
    history = db.get(HISTORY_KEY) or []
    history.append({"at": rep.started_at, "status": rep.status, "errors": len(rep.errors),
                    "warnings": len(rep.issues) - len(rep.errors), "incomplete": len(rep.failed_steps),
                    "missing_sources": len(rep.sources)})
    db.set(HISTORY_KEY, history[-HISTORY_DAYS:])


def record_failure(db: DB, error: str) -> None:
    """A check that crashed still leaves a row, so the history never shows a quiet gap as a good day."""
    history = db.get(HISTORY_KEY) or []
    history.append({"at": now(), "status": "failed", "error": error})
    db.set(HISTORY_KEY, history[-HISTORY_DAYS:])
