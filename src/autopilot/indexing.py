"""Measure what matters: the share of published pages Google has indexed, clicks to money pages, and conversions.

Every URL autopilot published or refreshed is checked with Search Console's URL Inspection API (2,000 calls per site
per day). A URL that has never been checked, or whose last check failed, is reported as "not verified", never as
indexed.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config, secret
from .crawl import classify
from .db import DB
from .topics import gsc_query, gsc_token

Log = Callable[[str], None]
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
NOT_VERIFIED = "not verified"


def _norm(url: str) -> str:
    return (url or "").rstrip("/")


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tracked_urls(db: DB) -> dict[str, str]:
    """url -> when it went live, for every page autopilot published or refreshed."""
    out: dict[str, str] = {}
    for a in db.articles("published"):
        if a["remote_url"] and a.get("publish_status") in ("publish", "published", None, ""):
            out[a["remote_url"]] = a["published_at"] or a["created_at"] or ""
    for r in db.refreshes("applied"):
        out.setdefault(r["target_url"], r["applied_at"] or r["created_at"] or "")
    return out


def state_of(row: dict[str, Any] | None) -> str:
    if not row or row.get("error") or not row.get("verdict"):
        return NOT_VERIFIED
    return "indexed" if row["verdict"] == "PASS" else "not indexed"


# -- URL Inspection -------------------------------------------------------------------

def quota_key(site: str, day: date | None = None) -> str:
    return f"inspect_quota:{site}:{(day or date.today()).isoformat()}"


def due_for_check(db: DB, cfg: Config, now: datetime | None = None) -> list[str]:
    """Never checked or failed first, then not-indexed pages (daily), then indexed ones past `recheck_days`."""
    now = now or datetime.now(timezone.utc)
    seen = db.inspections()
    never, retry, stale = [], [], []
    for url in tracked_urls(db):
        row = seen.get(url)
        checked = _parse(row["checked_at"]) if row else None
        if state_of(row) == NOT_VERIFIED:
            never.append(url)
        elif row["verdict"] != "PASS":
            if not checked or now - checked >= timedelta(days=1):
                retry.append(url)
        elif not checked or now - checked >= timedelta(days=cfg.measure.recheck_days):
            stale.append(url)
    return never + retry + stale


def inspect_urls(cfg: Config, db: DB, client: httpx.Client, log: Log = print,
                 token_fn: Callable[[Config], str] = gsc_token, limit: int | None = None) -> dict[str, int]:
    """Inspect due URLs within today's quota. Failures are stored, so those URLs show as not verified."""
    site = cfg.topics.gsc_site
    urls = due_for_check(db, cfg)
    counts = {"checked": 0, "indexed": 0, "not_indexed": 0, "failed": 0, "due": len(urls)}
    if not urls:
        log("URL Inspection: nothing due")
        return counts
    if not site:
        for url in urls:
            db.save_inspection(url, "", "", error="topics.gsc_site is not set")
        log(f"URL Inspection: set [topics] gsc_site to check {len(urls)} URL(s); they show as not verified")
        counts["failed"] = len(urls)
        return counts
    key = quota_key(site)
    used = int(db.get(key) or 0)
    remaining = max(0, cfg.measure.inspect_daily_quota - used)
    batch = urls[: min(remaining, limit or remaining)]
    if not batch:
        log(f"URL Inspection: today's quota of {cfg.measure.inspect_daily_quota} for {site} is used up")
        return counts
    try:
        token = token_fn(cfg)
    except Exception as exc:  # no credentials: record it so the report says "not verified" rather than guessing
        for url in batch:
            db.save_inspection(url, "", "", error=f"no Search Console token: {exc}"[:300])
        log(f"URL Inspection unavailable ({exc}); {len(batch)} URL(s) not verified")
        counts["failed"] = len(batch)
        return counts
    for url in batch:
        try:
            r = client.post(INSPECT_URL, headers={"Authorization": f"Bearer {token}"},
                            json={"inspectionUrl": url, "siteUrl": site}, timeout=60)
        except httpx.HTTPError as exc:
            db.save_inspection(url, "", "", error=str(exc)[:300])
            counts["failed"] += 1
            continue
        used += 1
        if r.status_code == 429:
            db.save_inspection(url, "", "", error="quota exceeded (HTTP 429)")
            counts["failed"] += 1
            log("URL Inspection: Google returned 429, stopping until tomorrow")
            used = cfg.measure.inspect_daily_quota
            break
        if r.status_code >= 400:
            db.save_inspection(url, "", "", error=f"HTTP {r.status_code}: {r.text[:200]}")
            counts["failed"] += 1
            continue
        status = (r.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
        verdict = status.get("verdict") or ""
        if not verdict:
            db.save_inspection(url, "", "", error="no verdict in the response")
            counts["failed"] += 1
            continue
        db.save_inspection(url, verdict, status.get("coverageState") or "", status.get("lastCrawlTime") or "")
        counts["checked"] += 1
        counts["indexed" if verdict == "PASS" else "not_indexed"] += 1
    db.set(key, used)
    log(f"URL Inspection: {counts['checked']} checked ({counts['indexed']} indexed, {counts['not_indexed']} not), "
        f"{counts['failed']} not verified; {max(0, cfg.measure.inspect_daily_quota - used)} calls left today")
    return counts


def index_rate(db: DB, min_age_days: int = 0, now: datetime | None = None) -> tuple[int, int, int]:
    """(indexed, verified, total) over tracked URLs live for at least `min_age_days`."""
    now = now or datetime.now(timezone.utc)
    seen = db.inspections()
    indexed = verified = total = 0
    for url, live in tracked_urls(db).items():
        when = _parse(live)
        if min_age_days and (not when or now - when < timedelta(days=min_age_days)):
            continue
        total += 1
        state = state_of(seen.get(url))
        if state != NOT_VERIFIED:
            verified += 1
            indexed += state == "indexed"
    return indexed, verified, total


# -- conversions ----------------------------------------------------------------------

def import_conversions_csv(db: DB, path: Path, site_url: str, source: str = "csv") -> int:
    """Rows of url,date,conversions (a header row is optional). Paths are joined to the site URL."""
    n = 0
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3 or not row[2].strip().replace(".", "", 1).isdigit():
                continue  # header or junk
            url, day, count = row[0].strip(), row[1].strip()[:10], float(row[2])
            if url.startswith("/"):
                url = site_url.rstrip("/") + url
            db.add_conversions(url, day, count, source)
            n += 1
    return n


def fetch_ga4_conversions(cfg: Config, db: DB, client: httpx.Client, days: int = 30,
                          token_fn: Callable[..., str] = gsc_token) -> int:
    """Key events by landing page from GA4 (`measure.ga4_property`), stored per day."""
    prop = cfg.measure.ga4_property
    if not prop:
        return 0
    token = token_fn(cfg, [GA4_SCOPE])
    end = date.today()
    r = client.post(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        headers={"Authorization": f"Bearer {token}"},
        json={"dateRanges": [{"startDate": (end - timedelta(days=days)).isoformat(), "endDate": end.isoformat()}],
              "dimensions": [{"name": "date"}, {"name": "landingPage"}], "metrics": [{"name": "keyEvents"}],
              "limit": 10000},
        timeout=60,
    )
    r.raise_for_status()
    n = 0
    for row in r.json().get("rows", []):
        d, page = (v["value"] for v in row["dimensionValues"])
        count = float(row["metricValues"][0]["value"] or 0)
        if not count or page in ("(not set)", ""):
            continue
        url = page if page.startswith("http") else cfg.site.url.rstrip("/") + page
        db.add_conversions(url, f"{d[:4]}-{d[4:6]}-{d[6:]}", count, "ga4")
        n += 1
    return n


# -- PostHog --------------------------------------------------------------------------

_SAFE = re.compile(r"^[A-Za-z0-9_$.:/\- ]{1,100}$")


def _lit(value: str) -> str:
    """A HogQL string literal for a config value (event names, site tags, hosts); anything odd is refused."""
    if not _SAFE.match(value or ""):
        raise ValueError(f"Unsupported character in PostHog config value {value!r}")
    return "'" + value + "'"


CHANNELS = ("search", "ai", "social", "other", "direct")
SEO_CHANNEL = "search"
_SEARCH = r"google|bing|duckduckgo|yahoo|baidu|yandex|ecosia|brave|startpage|qwant|naver"
_AI = r"chatgpt|openai|perplexity|claude|gemini|copilot|you"
_SOCIAL = (r"instagram|facebook|fb|threads|t|x|twitter|tiktok|youtube|youtu|linkedin|lnkd|reddit|pinterest|"
           r"nextdoor|snapchat|whatsapp|linktr")
_PAID = ("cpc", "ppc", "paid", "paidsearch", "paid_search", "paid-search", "display", "paid_social")


def _prop(name: str, what: str) -> str:
    if not re.match(r"^[A-Za-z0-9_$]{1,64}$", name):
        raise ValueError(f"Unsupported {what} {name!r}")
    return f"lower(coalesce(toString(properties.{name}), ''))"


def _host_match(expr: str, names: str) -> str:
    """`expr` (a URL or a bare source) names one of the hosts, with or without subdomains (www., m., l.)."""
    return f"match({expr}, '^(https?://)?([a-z0-9-]+[.])*({names})([.][a-z.]+)?(/|:|$)')"


def posthog_query(cfg: Config, since: date) -> str:
    """HogQL: per UTC day, landing path and channel, the distinct (browser, event) pairs among the conversion events.

    The landing path is the first-touch property the site registers once per browser (`posthog_landing_property`),
    then the session's entry path, then the page the event fired on. The channel comes from the first-touch UTM tags
    and referrer: search, ai, social, other (a referring site or a paid campaign) or direct (no referrer recorded).
    Test traffic and other hosts are left out."""
    m = cfg.measure
    host = (httpx.URL(cfg.site.url).host or "").lower().removeprefix("www.")
    prop = m.posthog_landing_property
    _prop(prop, "posthog_landing_property")
    ref = _prop(m.posthog_referrer_property, "posthog_referrer_property")
    med = _prop(m.posthog_utm_prefix + "medium", "posthog_utm_prefix")
    src = _prop(m.posthog_utm_prefix + "source", "posthog_utm_prefix")
    landing = (f"coalesce(nullIf(toString(properties.{prop}), ''), nullIf(toString(session.$entry_pathname), ''), "
               "toString(properties.$pathname))")
    social_paths = ", ".join(_lit(p) for p in m.posthog_social_landings) or "''"
    if not re.match(r"^[a-z0-9.-]+$", host):
        raise ValueError(f"Unsupported site host {host!r}")
    own = _host_match(ref, host.replace(".", "[.]"))
    channel = (
        "multiIf("
        f"{med} IN ({', '.join(_lit(x) for x in _PAID)}), 'other', "
        f"{med} IN ('social', 'social-media', 'social_media', 'sm', 'bio') OR {_host_match(src, _SOCIAL)} "
        f"OR {_host_match(ref, _SOCIAL)} OR {landing} IN ({social_paths}), 'social', "
        f"{med} = 'organic' OR {_host_match(src, _SEARCH)} OR {_host_match(ref, _SEARCH)}, 'search', "
        f"{_host_match(src, _AI)} OR {_host_match(ref, _AI)}, 'ai', "
        f"{src} != '' OR ({ref} != '' AND NOT {own}), 'other', "
        "'direct')"
    )
    events = ", ".join(_lit(e) for e in m.posthog_events)
    where = [f"timestamp >= toDateTime('{since.isoformat()} 00:00:00')", f"event IN ({events})",
             "lower(coalesce(toString(properties.analytics_test), '')) NOT IN ('true', '1')",
             f"lower(coalesce(toString(properties.$host), '')) IN ({_lit(host)}, {_lit('www.' + host)})"]
    if m.posthog_site:
        where.append(f"properties.site = {_lit(m.posthog_site)}")
    return (
        f"SELECT toDate(timestamp) AS day, {landing} AS landing, {channel} AS channel, "
        "uniq(distinct_id, event) AS conversions "
        f"FROM events WHERE {' AND '.join(where)} "
        "GROUP BY day, landing, channel ORDER BY day, landing, channel LIMIT 10000"
    )


def channel_of(source: str) -> str | None:
    """The channel of a stored conversion row (`posthog:search` → search); None for untagged sources (csv, ga4)."""
    return source.split(":", 1)[1] if ":" in source else None


def fetch_posthog_conversions(cfg: Config, db: DB, client: httpx.Client, days: int = 30,
                              today: date | None = None) -> int:
    """Conversion events from PostHog (`measure.posthog_*`), credited to each visitor's landing page, stored per day
    with source "posthog:<channel>". The window is replaced on every pull, so a re-run never double counts."""
    m = cfg.measure
    if not m.posthog_project_id:
        return 0
    if not m.posthog_events:
        raise ValueError("measure.posthog_events is empty")
    key = secret(m.posthog_api_key_env)
    since = (today or datetime.now(timezone.utc).date()) - timedelta(days=days)
    r = client.post(
        f"{m.posthog_host.rstrip('/')}/api/projects/{m.posthog_project_id}/query/",
        headers={"Authorization": f"Bearer {key}"},
        json={"query": {"kind": "HogQLQuery", "query": posthog_query(cfg, since)}, "name": "seo-autopilot conversions"},
        timeout=60,
    )
    if r.status_code in (401, 403):
        raise RuntimeError(f"PostHog refused the key in {m.posthog_api_key_env} (HTTP {r.status_code}); it needs "
                           f"the query:read scope on project {m.posthog_project_id}")
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"PostHog query failed: {body['error']}")
    cols = body.get("columns") or ["day", "landing", "channel", "conversions"]
    rows = [dict(zip(cols, row)) for row in body.get("results") or []]
    site = cfg.site.url.rstrip("/")
    db.clear_conversions("posthog", since.isoformat())
    totals: dict[tuple[str, str, str], float] = {}
    for row in rows:
        count = float(row.get("conversions") or 0)
        path = str(row.get("landing") or "").strip()
        if not count or not path:
            continue
        url = path if path.startswith("http") else site + "/" + path.lstrip("/")
        channel = str(row.get("channel") or "direct")
        k = (url, str(row["day"])[:10], channel if channel in CHANNELS else "other")
        totals[k] = totals.get(k, 0) + count
    for (url, day, channel), count in totals.items():
        db.add_conversions(url, day, count, f"posthog:{channel}")
    return len(totals)


# -- the report -----------------------------------------------------------------------

def start_date(db: DB) -> date:
    stored = db.get("measure_start")
    if stored:
        return date.fromisoformat(stored)
    first = min([a["created_at"] for a in db.articles() if a["created_at"]]
                + [r["created_at"] for r in db.refreshes() if r["created_at"]], default="")
    d = (_parse(first) or datetime.now(timezone.utc)).date()
    db.set("measure_start", d.isoformat())
    return d


def month_number(start: date, today: date | None = None) -> int:
    today = today or date.today()
    return max(1, (today.year - start.year) * 12 + today.month - start.month + (1 if today.day >= start.day else 0))


def money_urls(cfg: Config, db: DB) -> set[str]:
    return {_norm(p["url"]) for p in db.pages() if p.get("kind") == "money"}


def build_report(cfg: Config, db: DB, client: httpx.Client | None = None, days: int = 28, log: Log = print,
                 token_fn: Callable[[Config], str] = gsc_token, today: date | None = None) -> dict[str, Any]:
    """Index rate, clicks to money pages and conversions, against the 12-month targets."""
    mc = cfg.measure
    today = today or date.today()
    start = start_date(db)
    month = month_number(start, today)
    seen = db.inspections()
    tracked = tracked_urls(db)

    gsc_rows: dict[str, dict[str, Any]] = {}
    gsc_note = ""
    if client is not None and cfg.topics.gsc_enabled and cfg.topics.gsc_site:
        try:
            rows = gsc_query(client, token_fn(cfg), cfg.topics.gsc_site, days, ["page"], 5000)
            gsc_rows = {_norm(r["keys"][0]): r for r in rows}
        except Exception as exc:
            gsc_note = f"Search Console unavailable: {exc}"
    else:
        gsc_note = "Search Console not configured ([topics] gsc_enabled and gsc_site)"
    money = money_urls(cfg, db)
    money_clicks = None if gsc_note else int(sum(r.get("clicks", 0) for u, r in gsc_rows.items()
                                                  if u in money or classify(u, cfg.site.money_pages) == "money"))

    ph_note = ""
    if client is not None and mc.posthog_project_id:
        try:  # keeps PostHog conversions current; a failure leaves the last pull in place
            fetch_posthog_conversions(cfg, db, client, max(days, 30), today=today)
        except Exception as exc:
            ph_note = f"PostHog conversions not refreshed: {exc}"

    since = (today - timedelta(days=days)).isoformat()
    every = db.conversions(since)
    by_channel: dict[str, float] = {}
    for c in every:
        ch = channel_of(c["source"])
        if ch:
            by_channel[ch] = by_channel.get(ch, 0) + c["count"]
    # the SEO goal counts search conversions; rows with no channel (a CSV, GA4) can't be split, so they count as is
    conv = [c for c in every if channel_of(c["source"]) in (None, SEO_CHANNEL)]
    by_url: dict[str, float] = {}
    for c in conv:
        by_url[c["url"]] = by_url.get(c["url"], 0) + c["count"]
    tracked_norm = {_norm(u) for u in tracked}
    conv_tracked = sum(n for u, n in by_url.items() if _norm(u) in tracked_norm) if every else None
    conv_total = sum(c["count"] for c in conv) if every else None
    conv_money = sum(c["count"] for c in conv if _norm(c["url"]) in money) if every else None

    pages = []
    for url, live in sorted(tracked.items(), key=lambda x: x[1] or "", reverse=True):
        row = seen.get(url) or {}
        g = gsc_rows.get(_norm(url), {})
        pages.append({"url": url, "live": (live or "")[:10], "index": state_of(row),
                      "coverage": row.get("coverage_state") or row.get("error") or "",
                      "checked": (row.get("checked_at") or "")[:10],
                      "clicks": g.get("clicks", "") if not gsc_note else "", "impressions": g.get("impressions", "") if not gsc_note else ""})
    indexed = sum(1 for p in pages if p["index"] == "indexed")
    verified = sum(1 for p in pages if p["index"] != NOT_VERIFIED)
    elapsed = month / mc.horizon_months

    def target(value: float | None, goal: float) -> dict[str, Any]:
        """Where the metric is against its 12-month goal and the straight-line pace for this month."""
        if value is None:
            return {"value": None, "goal": goal, "pace": round(goal * min(1, elapsed), 1), "status": NOT_VERIFIED}
        pace = goal * min(1.0, elapsed)
        status = "on track" if value >= pace else "behind"
        return {"value": value, "goal": goal, "pace": round(pace, 1), "status": status}

    rate = indexed / verified if verified else None
    return {
        "month": month, "horizon": mc.horizon_months, "start": start.isoformat(), "days": days,
        "pages": pages, "published": len(tracked),
        "index": {"indexed": indexed, "verified": verified, "total": len(pages), "not_verified": len(pages) - verified,
                  "rate": rate, "target": mc.target_index_rate,
                  "status": NOT_VERIFIED if rate is None else ("on track" if rate >= mc.target_index_rate else "behind")},
        "money_clicks": target(money_clicks, mc.target_monthly_clicks),
        "conversions": {**target(conv_total, mc.target_monthly_conversions), "on_money_pages": conv_money,
                        "on_autopilot_pages": conv_tracked, "all": sum(c["count"] for c in every) if every else None,
                        "by_channel": {ch: by_channel[ch] for ch in CHANNELS if ch in by_channel},
                        "sources": sorted({c["source"].split(":")[0] for c in every}),
                        "by_url": [{"url": u, "count": n} for u, n in sorted(by_url.items(), key=lambda x: -x[1])]},
        "notes": [n for n in [gsc_note, ph_note, "" if every else "No conversions recorded: import a CSV "
                              "(`autopilot conversions import`), or set measure.posthog_project_id or "
                              "measure.ga4_property"] if n],
        "refreshes": {"pending": len(db.refreshes("pending_review")), "applied": len(db.refreshes("applied"))},
    }


def format_report(rep: dict[str, Any]) -> str:
    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0%}"

    def val(x: Any) -> str:
        return NOT_VERIFIED if x is None else (f"{x:g}" if isinstance(x, float) else str(x))

    ix, mc, cv = rep["index"], rep["money_clicks"], rep["conversions"]
    lines = [
        f"Month {rep['month']} of {rep['horizon']} (since {rep['start']}); figures cover the last {rep['days']} days.",
        f"Index rate: {pct(ix['rate'])} ({ix['indexed']} of {ix['verified']} checked pages indexed; "
        f"{ix['not_verified']} not verified). Target {pct(ix['target'])}: {ix['status']}.",
        f"Clicks to money pages: {val(mc['value'])}. Goal {mc['goal']:g}/month by month {rep['horizon']}, "
        f"pace for now {mc['pace']:g}: {mc['status']}.",
        f"Conversions{' from search' if cv.get('by_channel') else ''}: {val(cv['value'])}"
        + (f" ({val(cv['on_money_pages'])} landed on money pages, {val(cv.get('on_autopilot_pages'))} on autopilot "
           f"pages, from {', '.join(cv['sources'])})" if cv["value"] is not None else "")
        + f". Goal {cv['goal']:g}/month, pace for now {cv['pace']:g}: {cv['status']}.",
        f"Pages: {rep['published']} published or refreshed by autopilot; refreshes {rep['refreshes']['pending']} "
        f"pending, {rep['refreshes']['applied']} applied.",
    ]
    if cv.get("by_channel"):
        lines.append(f"All conversions: {val(cv['all'])} ("
                     + ", ".join(f"{ch} {val(n)}" for ch, n in cv["by_channel"].items()) + ")")
    top = cv.get("by_url") or []
    if top:
        lines.append(f"{'Search conversions' if cv.get('by_channel') else 'Conversions'} by landing page: " + ", ".join(f"{t['url']} {val(t['count'])}" for t in top[:5]))
    lines += [f"Note: {n}" for n in rep["notes"]]
    return "\n".join(lines)
