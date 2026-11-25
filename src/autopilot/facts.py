"""What the client's own site says about itself: the only allowed source for claims about the business.

The writer gets the relevant crawled page text (`pages.text`) so it can state real prices, ages and schedules, and the
scorer checks brand claims against the same text (see `score.unsupported_brand_claims`).
"""

from __future__ import annotations

from typing import Any

from .links import build_index
from .score import unsupported_brand_claims, unverified_domains
from .textutil import truncate_words
from .writer import Outline

GENERAL_KINDS = ("money", "about", "home")


def _block(pages: list[dict[str, Any]], words: int) -> str:
    return "\n\n".join(
        f"### {p['url']}\n{truncate_words(p.get('text') or p.get('description') or '', words)}" for p in pages
    )


def _usable(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in pages if p.get("kind") != "utility" and (p.get("text") or "").strip()]


def general_pages(pages: list[dict[str, Any]], n: int = 4) -> list[dict[str, Any]]:
    """Money pages first (they carry prices and programs), then about and home."""
    usable = _usable(pages)
    order = {k: i for i, k in enumerate(GENERAL_KINDS)}
    picked = sorted((p for p in usable if p.get("kind") in order), key=lambda p: (order[p["kind"]], p["url"]))
    return picked[:n]


def plan_facts(outline: Outline, pages: list[dict[str, Any]], method: str = "tfidf", per_section: int = 3,
               words: int = 300, min_score: float = 0.05) -> str:
    """Write each section's own fact block onto `section.facts` and return the general block for intro/closing/FAQ."""
    usable = _usable(pages)
    general = general_pages(usable)
    for s in outline.sections:
        s.facts = ""
    if not usable:
        return ""
    index = build_index(usable, method)
    money = [p for p in general if p.get("kind") == "money"][:1]
    for s in outline.sections:
        scores = index.scores(f"{outline.keyword} {s.h2} {' '.join(s.h3)} {s.notes}")
        ranked = [usable[i] for sc, i in sorted(((sc, i) for i, sc in enumerate(scores)), reverse=True) if sc >= min_score]
        chosen: list[dict[str, Any]] = []
        # the best matches, always the main money page (prices live there), then the next-best matches
        for p in ranked[:per_section - len(money)] + money + ranked:
            if p["url"] not in {c["url"] for c in chosen}:
                chosen.append(p)
            if len(chosen) >= per_section:
                break
        s.facts = _block(chosen, words)
    return _block(general, words)


def source_text(pages: list[dict[str, Any]], brand: dict[str, Any]) -> str:
    """Everything the site says, plus the Brand DNA fields that are names rather than model prose."""
    parts = [" ".join(str(p.get(k) or "") for k in ("title", "h1", "description", "text")) for p in pages]
    parts += [str(brand.get(k) or "") for k in ("business_name", "location", "cta")]
    parts += [str(o.get("name", "")) for o in brand.get("offerings", []) if isinstance(o, dict)]
    return "\n".join(parts)


class Grounding:
    """What a finished article may say about the business, and which websites it may name."""

    def __init__(self, pages: list[dict[str, Any]], brand: dict[str, Any], site_name: str, allowed_hosts: set[str]):
        self.names = [n for n in dict.fromkeys([brand.get("business_name") or "", site_name]) if n]
        self.source = source_text(pages, brand)
        self.allowed_hosts = {h.lower().removeprefix("www.") for h in allowed_hosts if h}

    def claims(self, markdown: str) -> list[str]:
        return unsupported_brand_claims(markdown, self.names, self.source)

    def domains(self, markdown: str) -> list[str]:
        return unverified_domains(markdown, self.allowed_hosts)
