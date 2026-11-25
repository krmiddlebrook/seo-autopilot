"""Brand DNA: one LLM call over ~10 representative pages, cached as JSON in SQLite."""

from __future__ import annotations

from typing import Any

from .db import DB
from .llm import LLM, complete_json
from .textutil import truncate_words

BRAND_KEY = "brand_dna"

SYSTEM = "You are a brand strategist and senior SEO editor. You study a website and describe its voice precisely."

PROMPT = """Study these pages from {site} and write its Brand DNA.

{pages}

Return JSON with exactly these keys:
{{
  "business_name": "string",
  "one_liner": "what the business does, for whom, in one sentence",
  "audience": "who reads and buys",
  "location": "service area if local, else empty string",
  "offerings": [{{"name": "product or service", "url": "page URL from above, or empty"}}],
  "tone": "3-6 adjectives",
  "voice_rules": ["concrete writing rules inferred from the copy, e.g. 'second person', 'short paragraphs'"],
  "vocabulary": ["distinctive words and phrases the brand uses"],
  "avoid": ["claims, words or topics to avoid"],
  "cta": "the primary call to action, with its URL if known",
  "themes": ["8 short search phrases (2-4 words) that real people type into Google and this site can credibly own, e.g. \"gutter cleaning cost\", not \"Home maintenance insights\""]
}}"""


def pick_pages(pages: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    order = {"home": 0, "about": 1, "money": 2, "page": 3, "post": 4, "utility": 9}
    usable = [p for p in pages if p.get("kind") != "utility" and (p.get("text") or "").strip()]
    usable.sort(key=lambda p: (order.get(p.get("kind", "page"), 5), -len(p.get("text") or "")))
    # keep a mix: at most 4 of any one kind
    picked, per_kind = [], {}
    for p in usable:
        k = p.get("kind", "page")
        if per_kind.get(k, 0) >= 4:
            continue
        per_kind[k] = per_kind.get(k, 0) + 1
        picked.append(p)
        if len(picked) >= n:
            break
    return picked


def build_brand_dna(llm: LLM, site_url: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    chosen = pick_pages(pages)
    if not chosen:
        raise RuntimeError("No crawlable page text found; run `autopilot crawl` and check the site URL")
    blocks = "\n\n".join(
        f"### {p['url']} ({p.get('kind')})\nTitle: {p.get('title')}\n{truncate_words(p.get('text') or '', 350)}"
        for p in chosen
    )
    dna = complete_json(llm, SYSTEM, PROMPT.format(site=site_url, pages=blocks))
    if not isinstance(dna, dict):
        raise RuntimeError("Brand DNA must be a JSON object")
    dna.setdefault("themes", [])
    dna.setdefault("offerings", [])
    dna["themes"] = [str(t) for t in dna["themes"]][:8]
    return dna


def load_brand(db: DB) -> dict[str, Any]:
    dna = db.get(BRAND_KEY)
    if not dna:
        raise RuntimeError("No Brand DNA yet. Run `autopilot onboard` first.")
    return dna


def brand_brief(dna: dict[str, Any]) -> str:
    """Compact Brand DNA block reused in every writing prompt."""
    offerings = ", ".join(o.get("name", "") for o in dna.get("offerings", []) if isinstance(o, dict))
    lines = [
        f"Business: {dna.get('business_name', '')}. {dna.get('one_liner', '')}",
        f"Audience: {dna.get('audience', '')}",
        f"Location: {dna.get('location', '')}" if dna.get("location") else "",
        f"Offerings: {offerings}" if offerings else "",
        f"Tone: {dna.get('tone', '')}",
        "Voice rules: " + "; ".join(dna.get("voice_rules", [])),
        "Use vocabulary like: " + ", ".join(dna.get("vocabulary", [])[:12]),
        "Avoid: " + "; ".join(dna.get("avoid", [])),
        f"Primary CTA: {dna.get('cta', '')}",
    ]
    return "\n".join(line for line in lines if line and not line.endswith(": "))
