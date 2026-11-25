"""Decide, before any writing, whether a topic deserves a new page.

* The keyword map gives every query exactly one page. A query that already has a page refreshes that page (or is
  left alone); a query too close to another page's query is blocked as cannibalization. Without Search Console the
  map starts empty, so crawled pages count too: a query whose words all appear in a page's title, H1 or slug (beyond
  the brand name every title shares) is routed to that page.
* The unique-value check names what a draft has that the SERP lacks: site data, a real price, a tool or a worked
  example. A draft with none of these is held for review.
* New pages are capped per rolling week, ramping slowly on a new domain and slowing when the index rate drops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .crawl import classify
from .db import DB
from .indexing import index_rate
from .refresh import refresh_mode
from .research import Research
from .textutil import jaccard, tokens


@dataclass
class Route:
    action: str  # write | refresh | skip
    reason: str
    url: str = ""
    mode: str = ""


def _norm(url: str) -> str:
    return (url or "").rstrip("/")


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# -- keyword map & routing ---------------------------------------------------------

def route_topic(cfg: Config, db: DB, topic: dict[str, Any], now: datetime | None = None) -> Route:
    """Refresh the page that owns the query, skip it, or (only with no owner and no near-duplicate) write."""
    now = now or datetime.now(timezone.utc)
    kw = topic["keyword"].strip().lower()
    owner = db.map_owner(kw) or topic.get("ranking_page") or ""
    if owner and classify(owner, cfg.site.money_pages) == "utility":
        owner = ""  # a contact or legal page ranking by accident doesn't own the query
    matched = ""
    if not owner:
        hit = crawl_match(cfg, db, kw)
        if hit:
            owner, phrase = hit
            if classify(owner, cfg.site.money_pages) == "home":
                return Route("skip", f"'{kw}' is a brand query; the home page ({owner}) owns it, no new page", owner)
            matched = f" (crawled page {owner} matches on '{phrase}')"
    if owner:
        draft = next((a for a in db.articles() if _norm(a["remote_url"]) == _norm(owner)
                      and a["status"] in ("pending_review", "approved")), None)
        if draft:
            return Route("skip", f"'{kw}' belongs to draft #{draft['id']}, which is waiting to be published", owner)
        if not cfg.refresh.enabled:
            return Route("skip", f"'{kw}' already has a page ({owner}){matched}; refresh is disabled, so no new page",
                         owner)
        position = topic.get("position")
        mode = refresh_mode(cfg.refresh, position, topic.get("clicks"), topic.get("impressions"))
        if mode is None:
            why = (f"ranks {position:g}, outside the refresh range" if position and position > cfg.refresh.max_position
                   else f"ranks {position:g} with a healthy click-through rate")
            return Route("skip", f"'{kw}' already has a page ({owner}) that {why}; no new page", owner)
        since = (now - timedelta(days=cfg.refresh.cooldown_days)).isoformat(timespec="seconds")
        recent = db.recent_refresh(owner, since)
        if recent:
            return Route("skip", f"{owner} has refresh #{recent['id']} ({recent['status']}) within "
                                 f"{cfg.refresh.cooldown_days} days{matched}", owner)
        return Route("refresh", f"'{kw}' already has a page{matched}; refreshing it ({mode})", owner, mode)
    clash = cannibal(cfg, db, kw)
    if clash:
        query, url = clash
        return Route("skip", f"'{kw}' would cannibalize {url}, which owns '{query}'. Map it with "
                             f"`autopilot map set \"{kw}\" URL` to refresh that page instead", url)
    return Route("write", f"no page owns '{kw}'")


def cannibal(cfg: Config, db: DB, kw: str) -> tuple[str, str] | None:
    """The mapped query (or article keyword) closest to `kw`, if it's close enough to compete for the same SERP."""
    threshold = cfg.topics.dedupe_threshold
    best: tuple[float, str, str] | None = None
    candidates = [(m["query"], m["url"]) for m in db.keyword_map()]
    candidates += [(a["keyword"], a["remote_url"]) for a in db.articles() if a["status"] not in ("rejected", "failed")
                   and a["keyword"]]
    for query, url in candidates:
        if query == kw:
            return query, url
        score = jaccard(kw, query)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, query, url)
    return (best[1], best[2]) if best else None


TITLE_SEP = re.compile(r"\s+[|\u2013\u2014-]\s+|\s*\|\s*")


def shared_segments(pages: list[dict[str, Any]]) -> set[str]:
    """Title segments after a separator that recur across pages: the brand suffix ("| Brookvale Wrestling")."""
    seen: dict[str, int] = {}
    for p in pages:
        for seg in {x.strip().lower() for x in TITLE_SEP.split(p.get("title") or "")[1:] if x.strip()}:
            seen[seg] = seen.get(seg, 0) + 1
    return {seg for seg, n in seen.items() if n >= 2}


def page_phrases(page: dict[str, Any], shared: set[str]) -> list[tuple[str, str]]:
    """(phrase, phrase without the brand suffix) for the title, H1 and slug."""
    title = page.get("title") or ""
    own = " | ".join(x for x in TITLE_SEP.split(title) if x.strip() and x.strip().lower() not in shared)
    path = (page.get("url") or "").split("://", 1)[-1].partition("/")[2].strip("/")
    slug = path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    h1 = page.get("h1") or ""
    return [(a, b) for a, b in ((title, own), (h1, h1), (slug, slug)) if a.strip()]


def crawl_match(cfg: Config, db: DB, kw: str) -> tuple[str, str] | None:
    """The crawled page that already targets `kw`: all of the query's words in its title, H1 or slug, matching on
    at least one word that isn't the brand (so "Contact Us | Brand" doesn't own every "brand ..." query). Titles are
    short, so a partial overlap ("gutter cleaning cost" vs a "Gutter Cleaning" service page) isn't a match. Among
    matches, the page whose own words (not its brand suffix) cover most of the query wins, then the tightest title.
    Utility pages and the home page never own a query."""
    every = db.pages()
    query = set(tokens(kw))
    shared = shared_segments(every)
    brand = set(tokens(cfg.site.name or "")).union(*(tokens(seg) for seg in shared))
    if query and not query - brand:  # a pure brand query belongs to the home page
        home = next((p["url"] for p in every if classify(p["url"], cfg.site.money_pages) == "home"), "")
        return (home, "the brand name") if home else None
    best: tuple[tuple[int, float], str, str] | None = None
    for p in every:
        if classify(p["url"], cfg.site.money_pages) in ("utility", "home"):
            continue
        for phrase, own in page_phrases(p, shared):
            words = set(tokens(phrase))
            if not (query <= words and (query - brand) & words):
                continue
            rank = (len(query & set(tokens(own))), len(query) / len(words))
            if best is None or rank > best[0]:
                best = (rank, p["url"], phrase)
    return (best[1], best[2]) if best else None


def map_conflicts(db: DB, threshold: float) -> list[tuple[str, str, str, str, float]]:
    """Pairs of near-identical queries mapped to different pages: likely cannibalization already live."""
    rows = db.keyword_map()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if _norm(a["url"]) != _norm(b["url"]):
                score = jaccard(a["query"], b["query"])
                if score >= threshold:
                    out.append((a["query"], a["url"], b["query"], b["url"], round(score, 2)))
    return sorted(out, key=lambda x: -x[4])


# -- unique value ------------------------------------------------------------------

PRICE = re.compile(r"(?:[$£€]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|dollars)\b)")
NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?%?")
TABLE_ROW = re.compile(r"^\s*\|.+\|\s*$", re.M)
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}", re.M)
TASK = re.compile(r"^\s*[-*]\s\[[ xX]\]\s", re.M)
EXAMPLE = re.compile(r"(?:for example|for instance|e\.g\.|say you|suppose|imagine|worked example|here's how the math)"
                     r"[^.\n]{0,220}?\d", re.I)


@dataclass
class UniqueValue:
    signals: dict[str, list[str]] = field(default_factory=dict)
    # site prices and numbers found when there were no search results to compare against: they may be on every
    # competing page, so they can't count as something the SERP lacks
    unverified: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return any(self.signals.values())

    def summary(self) -> str:
        found = "; ".join(f"{k.replace('_', ' ')}: {', '.join(v[:3])}" for k, v in self.signals.items() if v)
        guess = "; ".join(f"{k.replace('_', ' ')}: {', '.join(v[:3])}" for k, v in self.unverified.items() if v)
        if guess:
            guess = f"not verified (no competitor pages to compare against): {guess}"
        if not self.ok:
            base = "nothing the SERP lacks: no site data, real price, tool or worked example"
            return f"{guess}; {base} confirmed" if guess else base
        return f"{found}; {guess}" if guess else found

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "signals": self.signals, "unverified": self.unverified, "summary": self.summary()}


def _norm_num(s: str) -> str:
    return re.sub(r"[\s,]", "", s).replace("USD", "").replace("dollars", "").strip()


FACT_WINDOW = 200  # characters either side of a site number that must mention the topic


def topic_terms(keyword: str, pages: list[dict[str, Any]]) -> set[str]:
    """The keyword's words that say what it is about: words in most of the site's page titles ('wrestling' on a
    wrestling club's site) name the business, not the topic. Falls back to every keyword word."""
    words = set(tokens(keyword))
    titles = [set(tokens(f"{p.get('title') or ''} {p.get('h1') or ''}")) for p in pages]
    titles = [t for t in titles if t]
    if len(titles) >= 2:
        distinctive = {w for w in words if sum(w in t for t in titles) < len(titles) / 2}
        if distinctive:
            return distinctive
    return words


def relevant_site_text(pages: list[dict[str, Any]], keyword: str) -> str:
    """Site text whose numbers can count for this keyword: whole pages about the topic, plus the passages around a
    number elsewhere that mention it. A membership price on the club's join page is not data about weight classes."""
    def text(p: dict[str, Any]) -> str:
        return f"{p.get('title') or ''} {p.get('description') or ''} {p.get('text') or ''}"

    if not keyword.strip():
        return " ".join(text(p) for p in pages)
    terms = topic_terms(keyword, pages)
    need = len(terms) if len(terms) <= 2 else -(-len(terms) * 2 // 3)
    out = []
    for p in pages:
        if len(terms & set(tokens(f"{p.get('title') or ''} {p.get('h1') or ''}"))) >= need:
            out.append(text(p))
            continue
        body = text(p)
        for m in NUMBER.finditer(body):
            window = body[max(0, m.start() - FACT_WINDOW):m.end() + FACT_WINDOW]
            if len(terms & set(tokens(window))) >= need:
                out.append(window)
    return " ".join(out)


def unique_value(markdown: str, pages: list[dict[str, Any]], r: Research | None, keyword: str = "") -> UniqueValue:
    """What the draft offers that the top results don't. Only facts from the site's own pages count as site data, and
    only where those pages tie them to the keyword's topic.

    Prices and numbers need competitor pages to compare against. With none (no search provider, or the fetch found
    nothing) they are reported as not verified and don't count; a tool or a worked example still does."""
    site_text = relevant_site_text(pages, keyword or (r.keyword if r else ""))
    serp_text = ""
    if r:
        serp_text = " ".join([*(f"{c.title} {c.snippet} {' '.join(h for _, h in c.headings)}" for c in r.competitors),
                              *(f"{x.get('title', '')} {x.get('snippet', '')}" for x in r.serp),
                              *r.questions, *r.subtopics])
    site_prices = {_norm_num(m) for m in PRICE.findall(site_text)}
    serp_prices = {_norm_num(m) for m in PRICE.findall(serp_text)}
    body = re.sub(r"```.*?```", "", markdown, flags=re.S)
    prices = [m.strip() for m in PRICE.findall(body)
              if _norm_num(m) in site_prices and _norm_num(m) not in serp_prices]

    site_nums = {_norm_num(n) for n in NUMBER.findall(site_text)}
    serp_nums = {_norm_num(n) for n in NUMBER.findall(serp_text)}
    price_nums = {re.sub(r"[^\d.]", "", p) for p in prices}
    data = []
    for n in NUMBER.findall(re.sub(PRICE, " ", body)):
        k = _norm_num(n)
        digits = k.rstrip("%")
        if len(digits.replace(".", "")) < 2 or re.fullmatch(r"(19|20)\d\d", digits) or k in price_nums:
            continue  # single digits and years are everywhere
        if k in site_nums and k not in serp_nums:
            data.append(n)

    tools = []
    if TABLE_ROW.search(body) and TABLE_SEP.search(body):
        tools.append("comparison table")
    if TASK.search(body):
        tools.append("checklist")
    if re.search(r"<(form|iframe|input)\b", markdown, re.I):
        tools.append("interactive tool")
    examples = [m.group(0)[:80].strip() for m in EXAMPLE.finditer(body)]
    facts = {"price": list(dict.fromkeys(prices)), "site_data": list(dict.fromkeys(data))[:5]}
    if not (r and r.competitors):
        return UniqueValue({"tool": tools, "example": examples[:3]}, facts)
    return UniqueValue({**facts, "tool": tools, "example": examples[:3]})


# -- pacing ------------------------------------------------------------------------

@dataclass
class Pace:
    cap: int
    used: int
    reason: str

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)


def new_pages_since(db: DB, since: datetime) -> int:
    return sum(1 for a in db.articles() if a["status"] not in ("rejected", "failed")
               and (_parse(a["created_at"]) or since) >= since)


def weekly_cap(cfg: Config, db: DB, now: datetime | None = None) -> Pace:
    """How many new pages may be written in the rolling week that ends now, and why."""
    pc = cfg.pacing
    now = now or datetime.now(timezone.utc)
    articles = [a for a in db.articles() if a["status"] not in ("rejected", "failed")]
    content = sum(1 for p in db.pages() if p.get("kind") in ("post", "page")) + sum(
        1 for a in articles if a["status"] == "published")
    if content < pc.new_domain_pages and pc.ramp:
        first = min((_parse(a["created_at"]) for a in articles if _parse(a["created_at"])), default=now)
        week = (now - first).days // 7 + 1
        cap = min(pc.weekly_cap, pc.ramp[min(week, len(pc.ramp)) - 1])
        reason = f"new domain ({content} content pages): week {week} of the ramp allows {cap}"
    else:
        cap, reason = pc.weekly_cap, f"weekly cap {pc.weekly_cap}"
    indexed, verified, _ = index_rate(db, pc.index_min_age_days, now)
    if verified >= pc.min_sample:
        rate = indexed / verified
        if rate < pc.stop_below:
            cap, reason = 0, f"index rate {rate:.0%} is under {pc.stop_below:.0%}: no new pages until indexing recovers"
        elif rate < pc.slow_below:
            cap = max(1, cap // 2)
            reason += f", halved to {cap} because the index rate is {rate:.0%} (under {pc.slow_below:.0%})"
    used = new_pages_since(db, now - timedelta(days=7))
    return Pace(cap, used, reason)
