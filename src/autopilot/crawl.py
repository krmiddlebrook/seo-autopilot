"""Site crawl: sitemap.xml (and sitemap indexes) first, a same-host link crawl as fallback.

The fallback starts from the homepage, any configured seed URLs and common hub paths (/blog, /news...), so
JavaScript-rendered sites whose homepage has no <a> links still get crawled through their server-rendered
sections. Pages whose HTML is byte-identical to one already seen (an SPA serving its app shell for every
path, or duplicate URLs) are dropped.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from lxml import etree, html as lxml_html

SKIP_PATTERNS = re.compile(
    r"/(wp-admin|wp-json|feed|tag|author|cart|checkout|my-account|login|signin|account|search)(/|$)|"
    r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|xml|css|js)$",
    re.I,
)
UTILITY_PAGES = re.compile(r"/(privacy|terms|cookie|legal|disclaimer|sitemap|thank-you|404)", re.I)
MONEY_PAGES = re.compile(r"/(product|products|shop|store|service|services|pricing|plans|solutions|book|quote)", re.I)
HUB_PATHS = ("/blog", "/news", "/articles", "/posts", "/guides", "/resources", "/learn")


def classify(url: str, money_pages: tuple[str, ...] | list[str] = ()) -> str:
    """Page kind from its URL; `money_pages` are extra regexes (matched against the path) for product/CTA pages."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return "home"
    if UTILITY_PAGES.search(path):
        return "utility"
    if MONEY_PAGES.search(path) or any(re.search(p, path, re.I) for p in money_pages):
        return "money"
    if re.search(r"/(blog|news|articles|posts|guides|resources|learn)/", path + "/") or re.search(r"/\d{4}/\d{2}/", path):
        return "post"
    if re.search(r"/(about|team|story|company)", path, re.I):
        return "about"
    return "page"


def _parse_xml(content: bytes) -> etree._Element:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return etree.fromstring(content, parser=etree.XMLParser(recover=True))


def sitemap_urls(client: httpx.Client, sitemap_url: str, limit: int = 500, _depth: int = 0) -> list[tuple[str, str]]:
    """Return (url, lastmod) pairs, following sitemap indexes up to two levels deep."""
    try:
        r = client.get(sitemap_url)
        r.raise_for_status()
        root = _parse_xml(r.content)
    except (httpx.HTTPError, etree.XMLSyntaxError, OSError):
        return []
    if root is None:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tag = etree.QName(root).localname
    out: list[tuple[str, str]] = []
    if tag == "sitemapindex" and _depth < 2:
        locs = [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", ns) if loc.text]
        # post/page sitemaps first; image/tag/author sitemaps last
        locs.sort(key=lambda u: (bool(re.search(r"tag|author|image|video|category", u)), u))
        for loc in locs:
            out.extend(sitemap_urls(client, loc, limit - len(out), _depth + 1))
            if len(out) >= limit:
                break
    else:
        for url_el in root.findall(".//sm:url", ns):
            loc = url_el.find("sm:loc", ns)
            lastmod = url_el.find("sm:lastmod", ns)
            if loc is not None and loc.text:
                out.append((loc.text.strip(), (lastmod.text or "").strip() if lastmod is not None else ""))
    return out[:limit]


def find_sitemap(client: httpx.Client, site_url: str, sitemap_url: str = "", limit: int = 500) -> tuple[str, list[tuple[str, str]]]:
    """The first sitemap that lists any URLs, and its entries; ("", []) when the site has none that works."""
    for sm in [sitemap_url] if sitemap_url else discover_sitemaps(client, site_url):
        entries = sitemap_urls(client, sm, limit=limit)
        if entries:
            return sm, entries
    return "", []


def discover_sitemaps(client: httpx.Client, site_url: str) -> list[str]:
    candidates = []
    try:
        r = client.get(urljoin(site_url, "/robots.txt"))
        if r.status_code == 200:
            candidates += re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
    except httpx.HTTPError:
        pass
    candidates += [urljoin(site_url, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]
    return list(dict.fromkeys(candidates))


def extract_page(url: str, html_text: str, money_pages: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    doc = lxml_html.fromstring(html_text) if html_text.strip() else None
    title = h1 = description = ""
    if doc is not None:
        title = " ".join(doc.xpath("string(//title)").split())
        h1s = doc.xpath("//h1")
        h1 = " ".join(h1s[0].text_content().split()) if h1s else ""
        metas = doc.xpath("//meta[@name='description']/@content")
        description = metas[0].strip() if metas else ""
    text = trafilatura.extract(html_text, include_comments=False, include_tables=False) or ""
    return {"url": url, "title": title, "h1": h1, "description": description, "text": text, "kind": classify(url, money_pages)}


def headings(html_text: str) -> list[tuple[str, str]]:
    """(tag, text) for every h2/h3 in the document body."""
    try:
        doc = lxml_html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        return []
    out = []
    for el in doc.xpath("//h2|//h3"):
        text = " ".join(el.text_content().split())
        if 3 <= len(text) <= 160:
            out.append((el.tag, text))
    return out


def fetch_html(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        return r.text
    except httpx.HTTPError:
        return ""


def homepage_health(client: httpx.Client, site_url: str) -> tuple[int, int]:
    """(words of main text, same-host links) in the homepage's raw HTML, i.e. what a crawler sees before JavaScript."""
    body = fetch_html(client, site_url)
    if not body:
        return 0, 0
    host = urlparse(site_url).netloc
    try:
        hrefs = lxml_html.fromstring(body).xpath("//a/@href")
    except (etree.ParserError, ValueError):
        hrefs = []
    links = {urljoin(site_url, h).split("#")[0] for h in hrefs if urlparse(urljoin(site_url, h)).netloc == host}
    return len(extract_page(site_url, body)["text"].split()), len(links)


def link_crawl(client: httpx.Client, site_url: str, limit: int, seeds: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """Breadth-first same-host crawl from the homepage, seeds and hub paths, used when a site has no working
    sitemap. Returns {url: html}, skipping pages whose HTML repeats one already seen (an SPA's app shell)."""
    host = urlparse(site_url).netloc
    start = [site_url] + [urljoin(site_url, s) for s in seeds] + [urljoin(site_url, p) for p in HUB_PATHS]
    start = [u for u in dict.fromkeys(start) if urlparse(u).netloc == host]
    seen, queue, found, bodies = set(start), list(start), {}, set()
    while queue and len(found) < limit:
        url = queue.pop(0)
        body = fetch_html(client, url)
        digest = hashlib.sha1(body.encode()).digest()
        if not body or digest in bodies:
            continue
        bodies.add(digest)
        found[url] = body
        try:
            doc = lxml_html.fromstring(body)
        except (etree.ParserError, ValueError):
            continue
        for href in doc.xpath("//a/@href"):
            nxt = urljoin(url, href).split("#")[0].split("?")[0]
            if urlparse(nxt).netloc == host and nxt not in seen and not SKIP_PATTERNS.search(nxt):
                seen.add(nxt)
                queue.append(nxt)
    return found


def crawl_site(client: httpx.Client, site_url: str, sitemap_url: str = "", max_pages: int = 300, workers: int = 8,
               seeds: list[str] | tuple[str, ...] = (), money_pages: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    _, entries = find_sitemap(client, site_url, sitemap_url, limit=max_pages * 2)
    fetched: dict[str, str] = {}
    if not entries:
        fetched = link_crawl(client, site_url, max_pages, seeds)
        entries = [(u, "") for u in fetched]
    entries = [(u, m) for u, m in dict.fromkeys(entries) if not SKIP_PATTERNS.search(u)][:max_pages]

    def work(entry: tuple[str, str]) -> dict[str, Any] | None:
        url, lastmod = entry
        body = fetched.get(url) or fetch_html(client, url)
        if not body:
            return None
        page = extract_page(url, body, money_pages)
        page["lastmod"] = lastmod
        page["_digest"] = hashlib.sha1(body.encode()).digest()
        return page

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pages, digests = [], set()
        for page in pool.map(work, entries):
            if page and page["_digest"] not in digests:  # sitemap URLs that all serve the same shell
                digests.add(page.pop("_digest"))
                pages.append(page)
        return pages
