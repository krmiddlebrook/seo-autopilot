"""Refresh before writing anything new.

A query the site already ranks 4-20 for updates the page that ranks: sections the top five cover and the page
doesn't, an answer-first opening, and new internal links. A page in the top positions that gets few clicks gets a
title and description rewrite instead. Every refresh is stored as a diff held for review; nothing changes on the
site until `autopilot refresh apply`.
"""

from __future__ import annotations

import difflib
import hashlib
import html as html_lib
import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .brand import load_brand
from .config import Config, RefreshConfig
from .db import DB, now
from .facts import Grounding, plan_facts
from .links import MD_LINK, ensure_links, plan_internal_links, strip_unknown_links
from .llm import LLM, complete_json, complete_text
from .publish import PublishResult, RemoteDoc, can_update
from .render import to_html
from .research import Research, research
from .score import contains_keyword, invented_experience
from .textutil import clamp_text, jaccard, strip_markdown, tokens
from .writer import Outline, Section, _facts_instruction, business_name, system_prompt, write_section

Log = Callable[[str], None]

# Typical organic CTR by position (rounded from public click-curve studies). Used only to spot a snippet that
# underperforms its position, so the exact values matter less than the shape.
CTR_CURVE = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.07, 6: 0.05, 7: 0.04, 8: 0.03, 9: 0.03, 10: 0.025}

TAIL_HEADING = re.compile(r"faq|frequently asked|conclusion|final thoughts|next step|wrapping up|summary|key takeaways",
                          re.I)
SKIP_HEADING = re.compile(r"^(?:faq|frequently asked|conclusion|final thoughts|table of contents|contents|related|"
                          r"share|comments?|about the author|summary|references|sources|subscribe)", re.I)
HTML_LINK = re.compile(r"""<a\s[^>]*href=["']([^"']+)["']""", re.I)
SECTION_WORDS = 220


def expected_ctr(position: float) -> float:
    return CTR_CURVE.get(max(1, round(position)), 0.02)


def refresh_mode(rc: RefreshConfig, position: float | None, clicks: int | None, impressions: int | None) -> str | None:
    """content | meta | None (leave the page alone).

    No position (a manual keyword the map assigns to a page) means a content refresh."""
    if position is None:
        return "content"
    if rc.min_position <= position <= rc.max_position:
        return "content"
    if position < rc.min_position and (impressions or 0) >= rc.min_impressions:
        ctr = (clicks or 0) / max(1, impressions or 0)
        if ctr < rc.ctr_ratio * expected_ctr(position):
            return "meta"
    return None


def doc_hash(doc: RemoteDoc) -> str:
    return hashlib.sha256(f"{doc.title}\x00{doc.description}\x00{doc.body}".encode()).hexdigest()


# -- reading the existing page ----------------------------------------------------

def page_headings(body: str, fmt: str) -> list[str]:
    if fmt == "markdown":
        return [m.strip() for m in re.findall(r"^#{2,3}\s+(.+?)\s*#*$", body, flags=re.M)]
    return [html_lib.unescape(re.sub(r"<[^>]+>", "", m)).strip()
            for m in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", body, flags=re.I | re.S)]


def plain_text(body: str, fmt: str) -> str:
    if fmt == "markdown":
        return strip_markdown(body)
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S | re.I)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", body))


def linked_paths(body: str, fmt: str) -> set[str]:
    urls = [u for _, u in MD_LINK.findall(body)] if fmt == "markdown" else HTML_LINK.findall(body)
    if fmt == "markdown":
        urls += HTML_LINK.findall(body)
    return {(urlparse(u).path or u).rstrip("/") or "/" for u in urls}


def opening_words(body: str, fmt: str, n: int = 60) -> str:
    text = body
    if fmt == "markdown":
        text = re.sub(r"^#\s+.*$", "", text, count=1, flags=re.M)
    else:
        text = re.sub(r"<h1[^>]*>.*?</h1>", "", text, count=1, flags=re.S | re.I)
    return " ".join(plain_text(text, fmt).split()[:n])


# -- gap analysis against the top five --------------------------------------------

MIN_COMPETITOR_WORDS = 200  # a directory stub, a blocked fetch or a login wall says nothing about the topic
MIN_SHARE = 2  # one page's heading is that page's choice; a gap needs two ranking pages to agree
SELF_HEADING = re.compile(r"\b(?:our|we|we're|we've|choose us|join us|contact us|about us)\b", re.I)  # "What Our Members Say"


def brand_names(c: Any) -> set[str]:
    """A competitor's own name, squashed: from its host (eastfieldclub) and its title's site-name part."""
    host = urlparse(c.url).netloc.lower().removeprefix("www.").removeprefix("m.")
    names = {re.sub(r"[^a-z0-9]", "", host.split(".")[0])}
    parts = re.split(r"\s[|\u2013\u2014-]\s", c.title or "")
    if len(parts) > 1:
        names.add(re.sub(r"[^a-z0-9]", "", parts[-1].lower()))
    return {n for n in names if len(n) >= 4}


def find_gaps(r: Research, headings: list[str], text: str, limit: int) -> list[dict[str, Any]]:
    """Topics the ranking competitors cover and this page doesn't, most widely covered first.

    Only substantive pages count, a heading needs at least two of them behind it, and headings about a competitor
    itself (its name, "our members", "why choose us") are never gaps."""
    body_tokens = set(tokens(text))
    competitors = [c for c in r.competitors if c.words >= MIN_COMPETITOR_WORDS]
    topic = re.sub(r"[^a-z0-9]", "", r.keyword.lower())
    brands = {b for c in r.competitors for b in brand_names(c) if b not in topic and topic not in b}
    candidates: list[dict[str, Any]] = []

    def add(heading: str, source: str, weight: int) -> None:
        heading = heading.strip().rstrip(":")
        if len(tokens(heading)) < 2 or SKIP_HEADING.match(heading) or SELF_HEADING.search(heading):
            return
        squashed = re.sub(r"[^a-z0-9]", "", heading.lower())
        if any(b in squashed for b in brands):
            return
        for c in candidates:
            if jaccard(c["heading"], heading) >= 0.5:
                c["weight"] = max(c["weight"], weight)
                return
        candidates.append({"heading": heading, "source": source, "weight": weight})

    for st in r.subtopics:
        share = sum(1 for c in competitors if any(jaccard(st, h) >= 0.5 for _, h in c.headings))
        if share >= MIN_SHARE:
            add(st, f"covered by {share} of the top {len(competitors)}", 100 + share)
    counts: dict[str, int] = {}
    for c in competitors:
        for h in dict.fromkeys(h for tag, h in c.headings if tag == "h2"):
            counts[h] = sum(1 for o in competitors if any(jaccard(h, x) >= 0.5 for _, x in o.headings))
    for h, n in sorted(counts.items(), key=lambda x: -x[1]):
        if n >= MIN_SHARE:
            add(h, f"an H2 on {n} of the top {len(competitors)}", 50 + n)
    for q in r.questions:
        add(q, "a question searchers ask", 10)

    gaps = []
    for c in sorted(candidates, key=lambda c: -c["weight"]):
        cand = set(tokens(c["heading"]))
        if any(jaccard(c["heading"], h) >= 0.5 for h in headings):
            continue
        if cand and len(cand & body_tokens) / len(cand) >= 0.8:
            continue  # the page already talks about it, just not under its own heading
        gaps.append({"heading": c["heading"], "source": c["source"]})
        if len(gaps) >= limit:
            break
    return gaps


# -- prompts ------------------------------------------------------------------------

ANSWER_PROMPT = """Write an answer-first opening for the existing article "{h1}" (target query: "{keyword}").
The article currently opens with:
{opening}

- One paragraph, 40-70 words, no heading. Its first sentence answers "{keyword}" directly and uses that exact phrase.
- It goes above the current opening, so do not repeat what the opening already says.
{facts}Return markdown only."""

SNIPPET_PROMPT = """Rewrite the search snippet for {url}. It ranks at position {position} for "{keyword}" but gets few
clicks: {clicks} from {impressions} impressions ({ctr:.1%}, where about {expected:.0%} is usual for that position).
Current title: {title}
Current snippet text: {description}
The page opens with: {opening}

- Title: 45-60 characters. Include "{keyword}" or a close variant and say what the reader gets. No clickbait.
- Description: 130-155 characters. Answer the query directly, include "{keyword}", end with a reason to click.
- Promise only what the page already delivers.
Return JSON: {{"title": "...", "description": "..."}}"""


# -- editing the body ---------------------------------------------------------------

def insert_sections(body: str, fmt: str, sections: str) -> tuple[str, str]:
    """Put new sections before the FAQ / closing heading (or the JSON-LD block), else at the end."""
    if fmt == "markdown":
        for m in re.finditer(r"^#{2}\s+(.+)$", body, flags=re.M):
            if TAIL_HEADING.search(m.group(1)):
                return body[:m.start()] + sections.strip() + "\n\n" + body[m.start():], f"before '{m.group(1).strip()}'"
        return body.rstrip() + "\n\n" + sections.strip() + "\n", "at the end"
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", body, flags=re.I | re.S):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        if TAIL_HEADING.search(text):
            return body[:m.start()] + sections.strip() + "\n" + body[m.start():], f"before '{text.strip()}'"
    script = re.search(r"<script[^>]+application/ld\+json", body, flags=re.I)
    if script:
        return body[:script.start()].rstrip() + "\n" + sections.strip() + "\n" + body[script.start():], "before the schema"
    return body.rstrip() + "\n" + sections.strip() + "\n", "at the end"


def insert_intro(body: str, fmt: str, intro: str) -> str:
    if fmt == "markdown":
        m = re.match(r"\s*#\s+[^\n]*\n", body)
        cut = m.end() if m else 0
        return body[:cut] + ("\n" if cut else "") + intro.strip() + "\n\n" + body[cut:].lstrip("\n")
    m = re.match(r"\s*<h1[^>]*>.*?</h1>", body, flags=re.S | re.I)
    cut = m.end() if m else 0
    return body[:cut] + ("\n" if cut else "") + intro.strip() + "\n" + body[cut:].lstrip("\n")


def _blocks(text: str, fmt: str) -> list[str]:
    if fmt == "html":
        text = re.sub(r"(</(?:p|h[1-6]|ul|ol|table|li|blockquote|div|script|figure)>)", r"\1\n", text, flags=re.I)
    return [line for line in text.splitlines()]


def make_diff(url: str, old: RemoteDoc, title: str, description: str, body: str) -> str:
    lines = [f"--- {url} (live)", f"+++ {url} (refreshed)"]
    if title != old.title:
        lines += ["@@ title @@", f"-{old.title}", f"+{title}"]
    if description != old.description:
        lines += ["@@ description @@", f"-{old.description}", f"+{description}"]
    body_diff = list(difflib.unified_diff(_blocks(old.body, old.format), _blocks(body, old.format), lineterm="", n=2))
    lines += body_diff[2:]  # drop difflib's own ---/+++ header
    return "\n".join(lines) + "\n"


def _render(md: str, fmt: str) -> str:
    return md if fmt == "markdown" else to_html(md)


# -- planning -----------------------------------------------------------------------

def plan_refresh(cfg: Config, db: DB, client: httpx.Client, llm: LLM, publisher: Any, keyword: str, target_url: str,
                 mode: str, topic: dict[str, Any] | None = None, log: Log = print) -> int | None:
    """Fetch the live page, write the changes, store them as a diff held for review. Returns the refresh id."""
    if not can_update(publisher):
        raise RuntimeError(f"publish.target '{cfg.publish.target}' can't edit existing pages; "
                           "refreshes need wordpress, markdown, ghost or shopify")
    rc = cfg.refresh
    topic = topic or {}
    doc = publisher.fetch(target_url)
    brand = load_brand(db)
    business = business_name(brand, cfg)
    system = system_prompt(brand, cfg)
    h1 = doc.h1 or doc.title
    title, description, body = doc.title, doc.description, doc.body
    ops: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "mode": mode, "keyword": keyword, "url": target_url,
        "position": topic.get("position"), "clicks": topic.get("clicks"), "impressions": topic.get("impressions"),
        "warnings": [],
    }

    if mode == "meta":
        imp = int(topic.get("impressions") or 0)
        ctr = int(topic.get("clicks") or 0) / max(1, imp)
        pos = float(topic.get("position") or 1)
        report.update(ctr=round(ctr, 4), expected_ctr=expected_ctr(pos))
        data = complete_json(llm, system, SNIPPET_PROMPT.format(
            url=target_url, position=round(pos, 1), keyword=keyword, clicks=topic.get("clicks") or 0, impressions=imp,
            ctr=ctr, expected=expected_ctr(pos), title=doc.title, description=doc.description or "(none)",
            opening=opening_words(doc.body, doc.format, 80)))
        data = data if isinstance(data, dict) else {}
        title = clamp_text(str(data.get("title") or doc.title).strip(), 60)
        description = clamp_text(str(data.get("description") or doc.description).strip(), 160)
        if title != doc.title:
            ops.append({"op": "rewrite_title", "from": doc.title, "to": title})
        if description != doc.description:
            ops.append({"op": "rewrite_description", "from": doc.description, "to": description})
    else:
        log(f"Gap analysis for '{keyword}' against the top {cfg.search.results} results")
        r = research(cfg, client, keyword)
        report["competitors"] = [c.url for c in r.competitors]
        headings = page_headings(doc.body, doc.format)
        gaps = find_gaps(r, headings, plain_text(doc.body, doc.format), rc.max_sections)
        report["gaps"] = gaps
        log(f"{len(gaps)} gap(s): " + "; ".join(g["heading"] for g in gaps) if gaps else "No missing sections")

        pages = db.pages()
        target_path = (urlparse(target_url).path or "/").rstrip("/") or "/"
        already = linked_paths(doc.body, doc.format) | {target_path}
        link_pages = [p for p in pages if ((urlparse(p["url"]).path or "/").rstrip("/") or "/") not in already]
        outline = Outline(keyword=keyword, h1=h1, meta_title=doc.title, meta_description=doc.description,
                          slug=target_path.rsplit("/", 1)[-1], excerpt="",
                          sections=[Section(h2=g["heading"][:1].upper() + g["heading"][1:]) for g in gaps])
        _, closing = plan_internal_links(outline, link_pages, rc.max_new_links + 1, cfg.writing.embeddings)
        planned = [l for s in outline.sections for l in s.links] + closing
        planned = list({l["url"]: l for l in planned}.values())[:rc.max_new_links]
        keep = {l["url"] for l in planned}
        for s in outline.sections:
            s.links = [l for l in s.links if l["url"] in keep]
        if outline.sections:
            placed = {l["url"] for s in outline.sections for l in s.links}
            outline.sections[-1].links += [l for l in planned if l["url"] not in placed]
        general = plan_facts(outline, pages, cfg.writing.embeddings)
        allowed = {urlparse(cfg.site.url).netloc.lower().removeprefix("www.")} | {
            urlparse(u).netloc.lower().removeprefix("www.") for u in [x["url"] for x in r.serp] + [c.url for c in r.competitors]}
        grounding = Grounding(pages, brand, cfg.site.name, allowed)

        new_md: list[str] = []
        previous = " ".join(plain_text(doc.body, doc.format).split()[-80:])
        for s in outline.sections:
            md = write_section(llm, system, outline, s, r, SECTION_WORDS, previous, business)
            md, removed = strip_unknown_links(md, allowed)
            if removed:
                report["warnings"].append(f"unlinked made-up URL(s): {', '.join(removed)}")
            md = ensure_links(md, s.links)
            new_md.append(md)
            previous = " ".join(strip_markdown(md).split()[-80:])
        if not outline.sections and planned:
            new_md.append("Related reading: " + "; ".join(f"[{l['anchor']}]({l['url']})" for l in planned) + ".")
        if new_md:
            body, where = insert_sections(body, doc.format, _render("\n\n".join(new_md), doc.format))
            ops += [{"op": "add_section", "heading": s.h2, "where": where,
                     "why": next(g["source"] for g in gaps if g["heading"].lower() == s.h2.lower())}
                    for s in outline.sections]
        ops += [{"op": "add_link", "url": l["url"], "anchor": l["anchor"]} for l in planned]

        opening = opening_words(doc.body, doc.format)
        if not contains_keyword(opening, keyword, 0.8):
            intro = complete_text(llm, system, ANSWER_PROMPT.format(
                h1=h1, keyword=keyword, opening=opening or "(no opening paragraph)",
                facts=_facts_instruction(general, business))).strip()
            intro, _ = strip_unknown_links(intro, allowed)
            body = insert_intro(body, doc.format, _render(intro, doc.format))
            ops.append({"op": "answer_first_intro", "text": intro})
            new_md.insert(0, intro)
        added = "\n\n".join(new_md)
        claims, invented = grounding.claims(added), invented_experience(added)
        if claims:
            report["warnings"].append(f"{len(claims)} claim(s) about the business its own site doesn't support: {claims[0]}")
        if invented:
            report["warnings"].append(f"claims first-hand experience: {invented[0]}")

    if not ops:
        log(f"Nothing to change on {target_url}")
        return None
    report["summary"] = summarize(ops)
    refresh_id = db.insert_refresh({
        "topic_id": topic.get("id"), "keyword": keyword, "target_url": target_url, "mode": mode,
        "remote_id": doc.remote_id, "base_hash": doc_hash(doc),
        "old_title": doc.title, "new_title": title, "old_description": doc.description, "new_description": description,
        "old_body": doc.body, "new_body": body, "body_format": doc.format,
        "ops": json.dumps(ops), "diff": make_diff(target_url, doc, title, description, body),
        "report": json.dumps(report), "status": "pending_review",
    })
    path = write_diff(cfg, db, refresh_id)
    log(f"Refresh #{refresh_id} for {target_url} held for review ({report['summary']}); diff: {path}")
    for w in report["warnings"]:
        log(f"Check before applying: {w}")
    return refresh_id


def summarize(ops: list[dict[str, Any]]) -> str:
    n = lambda kind: sum(1 for o in ops if o["op"] == kind)  # noqa: E731
    parts = []
    if n("add_section"):
        parts.append(f"{n('add_section')} new section(s)")
    if n("answer_first_intro"):
        parts.append("answer-first intro")
    if n("add_link"):
        parts.append(f"{n('add_link')} new internal link(s)")
    if n("rewrite_title"):
        parts.append("new title")
    if n("rewrite_description"):
        parts.append("new description")
    return ", ".join(parts)


def write_diff(cfg: Config, db: DB, refresh_id: int) -> str:
    row = db.refresh(refresh_id)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.output_dir / f"refresh-{refresh_id}.diff"
    path.write_text(row["diff"])
    return str(path)


def apply_refresh(cfg: Config, db: DB, publisher: Any, refresh_id: int, log: Log = print) -> PublishResult:
    """Write an approved refresh to the CMS. Refuses when the live page changed after the refresh was planned."""
    row = db.refresh(refresh_id)
    if not row:
        raise RuntimeError(f"No refresh #{refresh_id}")
    if row["status"] not in ("pending_review", "approved"):
        raise RuntimeError(f"Refresh #{refresh_id} is {row['status']}")
    if not can_update(publisher):
        raise RuntimeError(f"publish.target '{cfg.publish.target}' can't edit existing pages")
    doc = publisher.fetch(row["target_url"])
    if doc_hash(doc) != row["base_hash"]:
        msg = "the live page changed after this refresh was planned; reject it and plan a new one"
        db.update_refresh(refresh_id, error=msg)
        raise RuntimeError(f"Refresh #{refresh_id}: {msg}")
    try:
        res = publisher.update(doc, row["new_title"], row["new_description"], row["new_body"])
    except Exception as exc:
        db.update_refresh(refresh_id, status="failed", error=str(exc)[:500])
        raise
    db.update_refresh(refresh_id, status="applied", applied_at=now(), error=res.git_error or "")
    db.map_set(row["keyword"], row["target_url"], "refresh")
    log(f"Refresh #{refresh_id} applied to {res.url or row['target_url']}")
    if res.git_error:
        log(f"Warning: {res.git_error}")
    return res
