"""Internal links by similarity (TF-IDF by default, sentence-transformers optional) plus authoritative external links."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Sequence
from urllib.parse import urlparse

from .textutil import tokens, truncate_words
from .writer import Outline

AUTHORITY = re.compile(r"(\.gov(\.[a-z]{2})?|\.edu|\.ac\.[a-z]{2}|wikipedia\.org|\.int|nih\.gov|who\.int)$")
MD_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")


def page_document(page: dict[str, Any]) -> str:
    title = page.get("title") or ""
    return " ".join([title] * 3 + [page.get("h1") or ""] * 2 + [page.get("description") or "", truncate_words(page.get("text") or "", 600)])


def short_title(page: dict[str, Any]) -> str:
    title = page.get("h1") or page.get("title") or page["url"]
    return re.split(r"\s+[|\-–—]\s+", title)[0].strip() or title


class TfidfIndex:
    def __init__(self, docs: Sequence[str]):
        self.doc_tokens = [Counter(tokens(d)) for d in docs]
        n = len(docs)
        df: Counter[str] = Counter()
        for c in self.doc_tokens:
            df.update(c.keys())
        self.idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}
        self.vectors = [self._vec(c) for c in self.doc_tokens]

    def _vec(self, counts: Counter[str]) -> dict[str, float]:
        v = {t: (1 + math.log(f)) * self.idf.get(t, 0.0) for t, f in counts.items() if t in self.idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def scores(self, query: str) -> list[float]:
        q = self._vec(Counter(tokens(query)))
        return [sum(w * doc.get(t, 0.0) for t, w in q.items()) for doc in self.vectors]


class EmbeddingIndex:  # pragma: no cover - optional heavy dependency
    def __init__(self, docs: Sequence[str], model: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)
        self.matrix = self.model.encode(list(docs), normalize_embeddings=True)

    def scores(self, query: str) -> list[float]:
        q = self.model.encode([query], normalize_embeddings=True)[0]
        return [float(x) for x in self.matrix @ q]


def build_index(pages: list[dict[str, Any]], method: str = "tfidf"):
    docs = [page_document(p) for p in pages]
    if method == "sentence-transformers":
        return EmbeddingIndex(docs)
    return TfidfIndex(docs)


def plan_internal_links(
    outline: Outline, pages: list[dict[str, Any]], max_links: int, method: str = "tfidf",
    ranking_page: str = "", min_score: float = 0.05,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Assign at most one internal link per section (best unused match). Returns (intro_links, closing_links).

    Section links are written onto `outline.sections[i].links`. A page that already ranks for the keyword is always
    linked (helps it climb), and the closing section gets the best-matching money page (product/service).
    """
    candidates = [p for p in pages if p.get("kind") not in ("utility", "home")]
    for s in outline.sections:
        s.links = []
    if not candidates or max_links <= 0:
        return [], []
    index = build_index(candidates, method)
    used: set[str] = set()
    budget = max_links

    def link(page: dict[str, Any], why: str) -> dict[str, str]:
        used.add(page["url"])
        return {"url": page["url"], "anchor": short_title(page), "why": why}

    whole = index.scores(f"{outline.keyword} {outline.h1} " + " ".join(s.h2 for s in outline.sections))
    intro_links: list[dict[str, str]] = []
    if ranking_page:
        rp = next((p for p in candidates if p["url"].rstrip("/") == ranking_page.rstrip("/")), None)
        if rp:
            intro_links.append(link(rp, "our existing page on this topic"))
            budget -= 1

    closing_links: list[dict[str, str]] = []
    money = sorted(
        ((whole[i], p) for i, p in enumerate(candidates) if p.get("kind") == "money" and p["url"] not in used),
        key=lambda x: x[0], reverse=True,
    )
    if money and budget > 0:
        closing_links.append(link(money[0][1], "the relevant product/service page"))
        budget -= 1

    ranked_sections = []
    for si, section in enumerate(outline.sections):
        scores = index.scores(f"{outline.keyword} {section.h2} {' '.join(section.h3)} {section.notes}")
        # money pages get a small boost so product links are not crowded out by blog posts
        boosted = [(s * (1.2 if candidates[i].get("kind") == "money" else 1.0), i) for i, s in enumerate(scores)]
        best = max(boosted)
        ranked_sections.append((best[0], si, sorted(boosted, reverse=True)))
    # give links to the sections with the strongest matches first
    for _, si, ranked in sorted(ranked_sections, reverse=True):
        if budget <= 0:
            break
        for score, i in ranked:
            if score < min_score:
                break
            if candidates[i]["url"] not in used:
                outline.sections[si].links.append(link(candidates[i], "related reading on our site"))
                budget -= 1
                break
    return intro_links, closing_links


def authority_tier(url: str) -> int:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if AUTHORITY.search(host):
        return 3
    if host.endswith(".org"):
        return 2
    return 0


US_STATES = set("""al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd
    oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc""".split())
FEDERAL = {"va.gov", "ed.gov"}  # federal agencies whose domain looks like a state code
LOCAL_HOST = re.compile(r"(?:county|cityof|townof|villageof|^city\.|^ci\.|parks|recreation|\brec\b)")
LOCAL_TEXT = re.compile(r"\b(?:city of|county|town of|village of|parks (?:and|&) rec\w*|recreation (?:department|center)|"
                        r"school district|municipal)\b", re.I)
COUNTRY_TLD = {"gb": "uk"}
GENERIC_CCTLDS = {"io", "co", "me", "tv", "ai", "fm", "ly"}
REGION_STOP = {"county", "city", "area", "region", "greater", "metro", "north", "south", "east", "west", "central",
               "serving", "nearby", "surrounding", "and", "the", "of", "usa", "us", "united", "states"}


def region_terms(location: str) -> list[str]:
    """'Brookvale, San Diego County, CA' -> ['brookvale', 'san diego', 'ca']: the place names a local source must mention."""
    out = []
    for part in re.split(r"[,;/()]|\band\b", location.lower()):
        words = [w for w in re.findall(r"[a-z]+", part) if w not in REGION_STOP]
        if words:
            out.append(" ".join(words))
    return out


def is_local_source(result: dict[str, str]) -> bool:
    """A city, county, state or parks-and-rec page: only useful to readers in that place."""
    host = urlparse(result["url"]).netloc.lower().removeprefix("www.")
    labels = host.split(".")
    if host in FEDERAL:
        return False
    if LOCAL_HOST.search(host) or LOCAL_TEXT.search(result.get("title", "")):
        return True
    # state and city governments: parks.ca.gov, ci.brookvale.ca.us, sandiego.ca.us
    return len(labels) >= 3 and labels[-1] in ("gov", "us") and labels[-2] in US_STATES


def in_region(result: dict[str, str], terms: list[str]) -> bool:
    host = urlparse(result["url"]).netloc.lower().replace("-", "")
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    for term in terms:
        if len(term) == 2:  # a state code only counts in the host: parks.ca.gov
            if f".{term}." in f".{host}.":
                return True
        elif term.replace(" ", "") in host or re.search(rf"\b{re.escape(term)}\b", text):
            return True
    return False


def foreign(url: str, country: str) -> bool:
    tld = urlparse(url).netloc.lower().rsplit(".", 1)[-1]
    want = COUNTRY_TLD.get(country.lower(), country.lower())
    return len(tld) == 2 and tld not in GENERIC_CCTLDS and tld != want


def _words(r: dict[str, str]) -> list[str]:
    path = urlparse(r["url"]).path.replace("_", " ").replace("-", " ")
    return tokens(f"{r.get('title', '')} {r.get('snippet', '')} {path}")


def narrowing_word(r: dict[str, str], keyword: str) -> str:
    """The word right before the keyword in a title ('Professional' in 'Professional wrestling weight classes')."""
    kw = tokens(keyword)
    title = tokens(r.get("title", ""))
    for i in range(1, len(title) - len(kw) + 1):
        if title[i:i + len(kw)] == kw:
            return title[i - 1]
    return ""


def on_topic(r: dict[str, str], keyword: str, results: list[dict[str, str]]) -> bool:
    """Covers most of the keyword's words, and isn't about a narrower topic nobody else in the results means:
    'Professional wrestling weight classes' is off topic when every other page is about school and youth wrestling."""
    kw = set(tokens(keyword))
    if not kw:
        return True
    need = len(kw) if len(kw) <= 2 else -(-len(kw) * 3 // 4)
    if len(kw & set(_words(r))) < need:
        return False
    word = narrowing_word(r, keyword)
    if word and word not in kw:
        others = [x for x in results if x["url"] != r["url"]]
        if others and not any(word in tokens(f"{x.get('title', '')} {x.get('snippet', '')}") for x in others):
            return False
    return True


def pick_external_links(results: list[dict[str, str]], own_host: str, n: int, country: str = "us",
                        location: str = "", keyword: str = "") -> list[dict[str, str]]:
    """Authoritative non-competitor sources only (.gov, .edu, Wikipedia, .org): never link to the pages you compete with.

    National sources in the site's country come first. A local government or parks-and-rec page is only used when it
    is in the business's own area (`location` from the Brand DNA), and other countries' sites are skipped. With a
    `keyword`, a source must be about that topic: a high-authority page on the wrong subject is worse than none.
    """
    terms = region_terms(location)

    def rank(r: dict[str, str]) -> tuple[int, int]:
        return authority_tier(r["url"]), int(is_local_source(r) and in_region(r, terms))

    seen, out = set(), []
    for r in sorted(results, key=rank, reverse=True):
        host = urlparse(r["url"]).netloc.lower().removeprefix("www.")
        if authority_tier(r["url"]) == 0 or host == own_host or host in seen or foreign(r["url"], country):
            continue
        if is_local_source(r) and not in_region(r, terms):
            continue
        if keyword and not on_topic(r, keyword, results):
            continue
        seen.add(host)
        out.append({"url": r["url"], "anchor": r.get("title") or host, "why": "authoritative source to cite"})
        if len(out) >= n:
            break
    return out


def find_links(markdown: str, site_url: str) -> tuple[list[str], list[str]]:
    host = urlparse(site_url).netloc.lower().removeprefix("www.")
    internal, external = [], []
    for _, url in MD_LINK.findall(markdown):
        if url.startswith("/"):
            internal.append(url)
            continue
        h = urlparse(url).netloc.lower().removeprefix("www.")
        (internal if h == host else external).append(url)
    return internal, external


def host_allowed(url_or_host: str, allowed: set[str]) -> bool:
    host = (urlparse(url_or_host).netloc or url_or_host).lower().removeprefix("www.")
    return any(host == a or host.endswith("." + a) for a in allowed)


def strip_unknown_links(markdown: str, allowed_hosts: set[str]) -> tuple[str, list[str]]:
    """Unlink any absolute URL whose host research never returned (a made-up domain); the anchor text stays."""
    removed: list[str] = []

    def fix(m: re.Match[str]) -> str:
        url = m.group(2)
        if url.startswith(("/", "#", "mailto:", "tel:")) or host_allowed(url, allowed_hosts):
            return m.group(0)
        removed.append(url)
        return m.group(1)

    return MD_LINK.sub(fix, markdown), removed


def ensure_links(markdown: str, planned: list[dict[str, str]]) -> str:
    """If the model dropped a planned link, append it to the end of the section that was supposed to hold it."""
    present = {u for _, u in MD_LINK.findall(markdown)}
    missing = [l for l in planned if l["url"] not in present]
    if not missing:
        return markdown
    extra = "; ".join(f"[{l['anchor']}]({l['url']})" for l in missing)
    return markdown.rstrip() + f"\n\nSee also: {extra}."
