"""SERP research: top results via SearXNG / Serper / Brave, then headings, lengths, subtopics and questions."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura

from .dataforseo import organic_search
from .config import Config, SearchConfig, secret
from .crawl import fetch_html, headings
from .textutil import tokens, word_count
from .topics import question_ideas

SKIP_DOMAINS = ("youtube.com", "facebook.com", "instagram.com", "tiktok.com", "pinterest.", "x.com", "twitter.com",
                "linkedin.com", "amazon.", "quora.com")


@dataclass
class Competitor:
    url: str
    title: str
    snippet: str = ""
    headings: list[tuple[str, str]] = field(default_factory=list)
    words: int = 0


@dataclass
class Research:
    keyword: str
    competitors: list[Competitor] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    subtopics: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    avg_words: int = 0
    serp: list[dict[str, str]] = field(default_factory=list)  # raw results, used to pick authoritative citations

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "competitors": [c.__dict__ for c in self.competitors],
            "questions": self.questions, "related": self.related, "subtopics": self.subtopics,
            "terms": self.terms, "avg_words": self.avg_words,
        }

    def brief(self) -> str:
        lines = [f"Top-ranking pages average {self.avg_words} words."]
        for c in self.competitors:
            h2s = [t for tag, t in c.headings if tag == "h2"][:10]
            lines.append(f"- {c.title} ({c.url}), {c.words} words. H2s: " + " | ".join(h2s))
        if self.subtopics:
            lines.append("Subtopics most competitors cover: " + "; ".join(self.subtopics))
        if self.terms:
            lines.append("Terms that recur across the top results: " + ", ".join(self.terms))
        if self.questions:
            lines.append("Questions searchers ask: " + " | ".join(self.questions[:10]))
        return "\n".join(lines)


def search(client: httpx.Client, sc: SearchConfig, query: str, language: str = "en", country: str = "us") -> dict[str, Any]:
    """Normalized: {"results": [{url,title,snippet}], "questions": [...], "related": [...]}"""
    provider = sc.provider.lower()
    if provider == "none":
        return {"results": [], "questions": [], "related": []}
    if provider == "dataforseo":
        return organic_search(client, query, language, country)
    if provider == "searxng":
        r = client.get(
            f"{sc.base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": language, "safesearch": 0},
        )
        if r.status_code == 403:
            raise RuntimeError(f"SearXNG at {sc.base_url} refused format=json (403). Mount a settings.yml with "
                               "`search: formats: [html, json]` (see README, SERP research) and restart it.")
        r.raise_for_status()
        data = r.json()
        return {
            "results": [{"url": x["url"], "title": x.get("title", ""), "snippet": x.get("content", "")} for x in data.get("results", [])],
            "questions": [],
            "related": [s for s in data.get("suggestions", []) if isinstance(s, str)],
        }
    if provider == "serper":
        r = client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": secret(sc.api_key_env or "SERPER_API_KEY")},
            json={"q": query, "gl": country, "hl": language, "num": 10},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "results": [{"url": x["link"], "title": x.get("title", ""), "snippet": x.get("snippet", "")} for x in data.get("organic", [])],
            "questions": [q["question"] for q in data.get("peopleAlsoAsk", []) if q.get("question")],
            "related": [q["query"] for q in data.get("relatedSearches", []) if q.get("query")],
        }
    if provider == "brave":
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": secret(sc.api_key_env or "BRAVE_API_KEY"), "Accept": "application/json"},
            params={"q": query, "country": country, "search_lang": language, "count": 10},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "results": [{"url": x["url"], "title": x.get("title", ""), "snippet": x.get("description", "")}
                        for x in data.get("web", {}).get("results", [])],
            "questions": [q["question"] for q in data.get("faq", {}).get("results", []) if q.get("question")],
            "related": [q["query"] for q in data.get("query", {}).get("related", []) if isinstance(q, dict) and q.get("query")],
        }
    raise ValueError(f"Unknown search provider '{sc.provider}'")


def common_subtopics(competitors: list[Competitor], min_share: float = 0.4, limit: int = 12) -> list[str]:
    """H2/H3 themes shared by several competitors: a heading counts once per page per distinctive token pair."""
    if not competitors:
        return []
    counts: Counter[str] = Counter()
    example: dict[str, str] = {}
    for c in competitors:
        seen: set[str] = set()
        for _, text in c.headings:
            toks = tokens(text)
            for i in range(len(toks)):
                for key in (toks[i], " ".join(toks[i:i + 2])):
                    if key and key not in seen:
                        seen.add(key)
                        counts[key] += 1
                        example.setdefault(key, text)
    need = max(2, round(len(competitors) * min_share))
    out: list[str] = []
    for key, n in counts.most_common():
        if n < need:
            break
        if " " in key or len(out) < limit // 2:
            label = example[key]
            if label not in out:
                out.append(label)
        if len(out) >= limit:
            break
    return out


def recurring_terms(texts: list[str], keyword: str, limit: int = 20) -> list[str]:
    kw = set(tokens(keyword))
    df: Counter[str] = Counter()
    for t in texts:
        toks = tokens(t)
        df.update({" ".join(toks[i:i + 2]) for i in range(len(toks) - 1)} | set(toks))
    need = max(2, len(texts) // 2 + (len(texts) % 2))
    terms = [t for t, n in df.most_common(400) if n >= need and not set(t.split()) <= kw and len(t) > 3]
    bigrams = [t for t in terms if " " in t]
    return (bigrams + [t for t in terms if " " not in t])[:limit]


def research(cfg: Config, client: httpx.Client, keyword: str) -> Research:
    own_host = urlparse(cfg.site.url).netloc.removeprefix("www.")
    serp = search(client, cfg.search, keyword, cfg.site.language, cfg.site.country)
    picked = []
    for res in serp["results"]:
        host = urlparse(res["url"]).netloc.removeprefix("www.")
        if host == own_host or any(d in host for d in SKIP_DOMAINS) or res["url"].endswith(".pdf"):
            continue
        picked.append(res)
        if len(picked) >= cfg.search.results:
            break

    def fetch(res: dict[str, str]) -> tuple[Competitor, str]:
        body = fetch_html(client, res["url"])
        text = (trafilatura.extract(body) or "") if body else ""
        return Competitor(res["url"], res["title"], res.get("snippet", ""), headings(body) if body else [], word_count(text)), text

    with ThreadPoolExecutor(max_workers=5) as pool:
        fetched = list(pool.map(fetch, picked))
    competitors = [c for c, _ in fetched]
    texts = [t for _, t in fetched if t]
    lengths = [c.words for c in competitors if c.words > 200]

    questions = list(serp["questions"])
    if len(questions) < 5:
        questions += question_ideas(client, keyword, cfg.site.language, cfg.site.country)
    return Research(
        keyword=keyword,
        competitors=competitors,
        questions=list(dict.fromkeys(q.strip().rstrip("?") + "?" for q in questions if q.strip()))[:12],
        related=serp["related"][:10],
        subtopics=common_subtopics(competitors),
        terms=recurring_terms(texts, keyword),
        avg_words=round(sum(lengths) / len(lengths)) if lengths else 0,
        serp=serp["results"],
    )
