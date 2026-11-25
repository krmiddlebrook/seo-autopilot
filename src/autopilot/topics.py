"""Topic planning: GSC striking-distance queries + Google Autocomplete, optional Semrush volume/KD, deduped in SQLite."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import Config, TopicsConfig, secret
from .dataforseo import keyword_metrics
from .db import DB
from .textutil import jaccard, tokens

AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
QUESTION_PREFIXES = ("how to", "what is", "why", "can you", "best", "cost of")


# -- Google Search Console --------------------------------------------------

def gsc_token(cfg: Config, scopes: list[str] | None = None) -> str:
    """OAuth access token from a service-account key or an OAuth client secret (token cached next to the config).

    `scopes` defaults to Search Console read-only; GA4 asks for analytics.readonly with the same credentials."""
    scopes = scopes or [GSC_SCOPE]
    try:
        from google.auth.transport.requests import Request
    except ImportError as err:  # pragma: no cover - optional extra
        raise RuntimeError("GSC needs the optional deps: `uv sync --extra gsc`") from err

    cred_path = cfg.resolve(cfg.topics.gsc_credentials)
    if not cred_path.exists():
        raise RuntimeError(f"GSC credentials not found at {cred_path}")
    info = json.loads(cred_path.read_text())
    if info.get("type") == "service_account":
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        from google.oauth2.credentials import Credentials

        token_path = cfg.root / (".gsc-token.json" if scopes == [GSC_SCOPE] else ".google-analytics-token.json")
        creds = Credentials.from_authorized_user_file(str(token_path), scopes) if token_path.exists() else None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                from google_auth_oauthlib.flow import InstalledAppFlow

                creds = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes).run_local_server(port=0)
            token_path.write_text(creds.to_json())
        return creds.token
    creds.refresh(Request())
    return creds.token


def gsc_query(
    client: httpx.Client, token: str, site: str, days: int, dimensions: list[str], row_limit: int = 5000,
    filters: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    end = date.today() - timedelta(days=2)  # GSC data lags ~2 days
    body: dict[str, Any] = {
        "startDate": (end - timedelta(days=days)).isoformat(),
        "endDate": end.isoformat(),
        "dimensions": dimensions,
        "rowLimit": row_limit,
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    r = client.post(
        f"https://www.googleapis.com/webmasters/v3/sites/{quote(site, safe='')}/searchAnalytics/query",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    r.raise_for_status()
    return r.json().get("rows", [])


def striking_distance(rows: list[dict[str, Any]], tc: TopicsConfig, brand_terms: list[str]) -> list[dict[str, Any]]:
    """Queries ranking at positions min..max with enough impressions: the cheapest wins available."""
    brand_tokens = {t for term in brand_terms for t in tokens(term)}
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        query, page = row["keys"][0], row["keys"][1] if len(row["keys"]) > 1 else ""
        pos, imp = float(row.get("position", 99)), int(row.get("impressions", 0))
        if not (tc.min_position <= pos <= tc.max_position) or imp < tc.min_impressions:
            continue
        if brand_tokens and set(tokens(query)) & brand_tokens:
            continue  # branded queries are already yours
        if len(tokens(query)) < 2:
            continue
        cur = best.get(query)
        if cur is None or imp > cur["impressions"]:
            best[query] = {
                "keyword": query, "impressions": imp, "clicks": int(row.get("clicks", 0)),
                "position": round(pos, 1), "ranking_page": page,
            }
    return list(best.values())


def refresh_candidates(rows: list[dict[str, Any]], cfg: Config, brand_terms: list[str]) -> list[dict[str, Any]]:
    """Queries whose ranking page should be refreshed: positions in the refresh range, or a top spot with a weak CTR."""
    from .refresh import refresh_mode  # local: refresh pulls in the writer, which topics doesn't otherwise need

    rc = cfg.refresh
    wide = TopicsConfig(min_position=1.0, max_position=rc.max_position, min_impressions=min(cfg.topics.min_impressions,
                                                                                            rc.min_impressions))
    return [t for t in striking_distance(rows, wide, brand_terms)
            if refresh_mode(rc, t["position"], t["clicks"], t["impressions"]) is not None]


def map_queries(db: DB, rows: list[dict[str, Any]]) -> int:
    """Give each GSC query its one page: the page with the most impressions. Manual and article entries win."""
    best: dict[str, tuple[int, str]] = {}
    for row in rows:
        if len(row.get("keys", [])) < 2:
            continue
        query, page, imp = row["keys"][0].strip().lower(), row["keys"][1], int(row.get("impressions", 0))
        if query not in best or imp > best[query][0]:
            best[query] = (imp, page)
    current = {m["query"]: m for m in db.keyword_map()}
    n = 0
    for query, (_, page) in best.items():
        cur = current.get(query)
        if cur and (cur["source"] != "gsc" or cur["url"] == page):
            continue
        db.map_set(query, page, "gsc")
        n += 1
    return n


# -- Autocomplete / questions -------------------------------------------------

def autocomplete(client: httpx.Client, query: str, language: str = "en", country: str = "us") -> list[str]:
    try:
        r = client.get(AUTOCOMPLETE_URL, params={"client": "firefox", "q": query, "hl": language, "gl": country})
        r.raise_for_status()
        data = json.loads(r.content.decode(r.encoding or "utf-8", errors="replace"))
        return [s for s in data[1] if isinstance(s, str)]
    except (httpx.HTTPError, ValueError, IndexError, KeyError):
        return []


def question_ideas(client: httpx.Client, seed: str, language: str = "en", country: str = "us") -> list[str]:
    """People-Also-Ask-style questions from autocomplete with question prefixes."""
    out: list[str] = []
    for prefix in QUESTION_PREFIXES:
        out += autocomplete(client, f"{prefix} {seed}", language, country)
    return list(dict.fromkeys(q for q in out if len(tokens(q)) >= 2))


# -- Semrush (optional) ---------------------------------------------------------

def semrush_metrics(client: httpx.Client, api_key: str, keywords: list[str], database: str = "us") -> dict[str, dict[str, float]]:
    """Volume and keyword difficulty via the Semrush API (phrase_this). Costs API units; skipped when unset."""
    out: dict[str, dict[str, float]] = {}
    for kw in keywords:
        try:
            r = client.get(
                "https://api.semrush.com/",
                params={"type": "phrase_this", "key": api_key, "phrase": kw, "database": database,
                        "export_columns": "Ph,Nq,Kd"},
            )
            lines = r.text.strip().splitlines()
            if r.status_code != 200 or len(lines) < 2 or lines[0].startswith("ERROR"):
                continue
            _, volume, kd = lines[1].split(";")[:3]
            out[kw] = {"volume": int(volume or 0), "kd": float(kd or 0)}
        except (httpx.HTTPError, ValueError):
            continue
    return out


# -- Scoring & dedupe -------------------------------------------------------------

def topic_score(t: dict[str, Any]) -> float:
    if t.get("volume") is not None and t.get("kd") is not None:
        # traffic potential discounted by difficulty
        return round(float(t["volume"]) / (float(t["kd"]) + 10), 2) + (5 if t.get("impressions") else 0)
    if t.get("impressions"):
        # more impressions and a position closer to page one = bigger, faster win
        return round(math.log1p(t["impressions"]) * 10 * (31 - min(t.get("position", 30), 30)) / 23, 2)
    return 1.0 + 0.1 * len(tokens(t["keyword"]))  # long-tail autocomplete ideas


def is_duplicate(keyword: str, existing: list[str], threshold: float) -> bool:
    return any(jaccard(keyword, e) >= threshold for e in existing)


def refresh_topics(
    cfg: Config, db: DB, client: httpx.Client, brand: dict[str, Any],
    log: Callable[[str], None] = print, token_fn: Callable[[Config], str] = gsc_token,
) -> int:
    tc = cfg.topics
    candidates: list[dict[str, Any]] = []
    if tc.gsc_enabled:
        token = token_fn(cfg)
        rows = gsc_query(client, token, tc.gsc_site, tc.gsc_days, ["query", "page"])
        brand_terms = [brand.get("business_name", ""), cfg.site.name]
        found = striking_distance(rows, tc, brand_terms)
        log(f"GSC: {len(found)} striking-distance queries (positions {tc.min_position:g}-{tc.max_position:g})")
        if cfg.refresh.enabled:
            seen = {f["keyword"] for f in found}
            extra = [t for t in refresh_candidates(rows, cfg, brand_terms) if t["keyword"] not in seen]
            if extra:
                log(f"GSC: {len(extra)} more queries whose ranking page needs a refresh")
            found += extra
        mapped = map_queries(db, rows)
        if mapped:
            log(f"Keyword map: {mapped} queries assigned to the page Google ranks for them")
        candidates += [{**t, "source": "gsc"} for t in found]
    if tc.autocomplete:
        seeds = list(dict.fromkeys([*tc.seeds, *brand.get("themes", [])]))
        location = brand.get("location") or ""
        ideas: list[str] = []
        for seed in seeds:
            ideas += autocomplete(client, seed, cfg.site.language, cfg.site.country)
            if location and len(location) < 40:
                ideas += autocomplete(client, f"{seed} {location}", cfg.site.language, cfg.site.country)
        ideas = [i for i in dict.fromkeys(ideas) if len(tokens(i)) >= 2]
        log(f"Autocomplete: {len(ideas)} ideas from {len(seeds)} seeds")
        candidates += [{"keyword": i, "source": "autocomplete"} for i in ideas]

    provider = tc.metrics_provider or ("semrush" if tc.semrush_api_key_env else "none")
    if candidates and provider != "none":
        keywords = [c["keyword"] for c in candidates]
        if provider == "dataforseo":
            metrics = keyword_metrics(client, keywords, cfg.site.language, cfg.site.country)
        else:
            key = secret(tc.semrush_api_key_env or "SEMRUSH_API_KEY")
            metrics = semrush_metrics(client, key, keywords[:50], cfg.site.country)
        for c in candidates:
            c.update(metrics.get(c["keyword"], {}))
        log(f"{provider}: keyword metrics for {len(metrics)} keywords")

    existing = list(db.known_titles())
    added = 0
    for c in sorted(candidates, key=topic_score, reverse=True):
        if c["source"] != "gsc" and is_duplicate(c["keyword"], existing, tc.dedupe_threshold):
            continue
        extra = {k: c[k] for k in ("impressions", "clicks", "position", "volume", "kd", "ranking_page") if c.get(k) is not None}
        if db.add_topic(c["keyword"], c["source"], topic_score(c), **extra):
            added += 1
            existing.append(c["keyword"])
    return added


def pick_topic(db: DB, threshold: float = 0.6) -> dict[str, Any] | None:
    """Highest-scoring queued topic that doesn't overlap anything already written."""
    existing = [t for t in db.known_titles()]
    for topic in db.topics("queued", limit=500):
        written = [e for e in existing if e.lower() != topic["keyword"]]
        if topic["source"] != "gsc" and is_duplicate(topic["keyword"], written, threshold):
            db.set_topic_status(topic["id"], "skipped")
            continue
        return topic
    return None
