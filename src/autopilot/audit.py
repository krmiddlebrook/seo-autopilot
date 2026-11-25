"""Core-page audits: each day, check one money or home page against the searches it should win and propose edits.

New blog posts rarely move a small site. The pages that earn sign-ups (home, programs, pricing, contact) do, so each
daily run picks the core page whose last audit is oldest, pulls the Search Console queries it shows up for, runs fixed
checks (snippet wording, CTR for its position, striking-distance queries, thin copy, clicks that never convert) and
asks the model for a better title, description and H1 plus concrete edits. Nothing is published: the owner applies the
edits wherever the page lives (a CMS, a Replit app, hand-written HTML), then `autopilot audits done ID` pings IndexNow
and reminds them to request indexing in Search Console. The next audit of that page reports what changed since.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from lxml import etree, html as lxml_html

from .brand import BRAND_KEY
from .config import Config
from .crawl import extract_page, fetch_html, headings
from .db import DB, now
from .llm import LLM, complete_json
from .refresh import expected_ctr
from .research import research
from .textutil import clamp_text, tokens, word_count

Log = Callable[[str], None]
QueryRows = Callable[[str], list[dict[str, Any]]]

LAST_KEY = "audit_last_run"
CORE_KINDS = ("home", "money")
TITLE_RANGE = (30, 60)
DESCRIPTION_RANGE = (70, 160)
NUMBER = re.compile(r"\d[\d,.:]*\d|\d")
SEPARATORS = (" | ", " - ", " – ", " · ", ": ")
BREAK = re.compile(r"<br\s*/?>", re.I)
# Counts in the edit instructions themselves ("add 2-3 sentences", "5 questions"), not facts for the page.
INSTRUCTION_COUNT = re.compile(
    r"\b\d+\s*(?:-|–|to)\s*\d+\b|\b\d+\s+(?:short\s+|answered\s+|more\s+)?"
    r"(?:sentences?|questions?|lines?|words?|paragraphs?|bullets?|points?|links?|sections?|items?)\b", re.I)

SYSTEM = """You are the SEO editor for {business}. You improve one existing page so it wins the searches it already \
shows up for and turns more visitors into enquiries. Write in {language}.

Rules:
- Use only facts stated on the page. Never invent prices, dates, results, names, numbers or guarantees.
- When an edit needs a fact the page does not state, add the question to "needs_owner" instead of guessing.
- Keep the page's own voice. Plain, specific words; no hype.
- Title: at most 60 characters, the main search first, the business name last if it fits.
- Description: 120-155 characters, says who it is for, what they get and the next step.
- H1: one clear line that names the main search in natural words.
- Edits: at most 6, each one concrete (what to add, move or reword, and where), most valuable first."""

PROMPT = """Page: {url} ({kind} page)
Main search: "{primary}"

Search Console, last {days} days (query | clicks | impressions | CTR | position):
{queries}

Problems found:
{findings}

Current title: {title}
Current description: {description}
Current H1: {h1}
Current headings: {headings}

What the top results for "{primary}" cover:
{competitors}
Questions searchers ask: {questions}

Page text:
\"\"\"
{text}
\"\"\"

Reply with JSON only:
{{"title": "...", "description": "...", "h1": "...",
  "edits": [{{"section": "where on the page", "change": "what to add or reword", "why": "which search or problem it fixes"}}],
  "needs_owner": ["a question for the owner when an edit needs a fact the page lacks"]}}"""


@dataclass
class Query:
    query: str
    clicks: int
    impressions: int
    ctr: float
    position: float


def to_queries(rows: list[dict[str, Any]]) -> list[Query]:
    out = [Query(r["keys"][0], int(r.get("clicks", 0)), int(r.get("impressions", 0)), float(r.get("ctr", 0.0)),
                 float(r.get("position", 0.0))) for r in rows if r.get("keys")]
    return sorted(out, key=lambda q: (-q.impressions, -q.clicks, q.query))


def is_brand(query: str, brand_terms: list[str]) -> bool:
    q = query.lower()
    q_tokens = set(tokens(query))
    for term in brand_terms:
        t = (term or "").lower().strip()
        if not t:
            continue
        t_tokens = set(tokens(t))
        if t in q or (q_tokens and t_tokens and q_tokens <= t_tokens):
            return True
    return False


def primary_query(queries: list[Query], brand_terms: list[str]) -> Query | None:
    """The non-brand query with the most impressions; the top query when every query is the brand."""
    if not queries:
        return None
    return next((q for q in queries if not is_brand(q.query, brand_terms)), queries[0])


def _audit_time(a: dict[str, Any]) -> datetime:
    t = datetime.fromisoformat(a["created_at"])
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def core_pages(db: DB) -> list[dict[str, Any]]:
    return [p for p in db.pages() if p.get("kind") in CORE_KINDS]


def pick_page(cfg: Config, db: DB, at: datetime | None = None) -> dict[str, Any] | None:
    """Never-audited core pages first (home before money pages), then the one audited longest ago, outside cooldown."""
    at = at or datetime.now(timezone.utc)
    last: dict[str, datetime] = {}
    for a in db.audits():
        t = _audit_time(a)
        last[a["url"]] = max(t, last.get(a["url"], t))
    due = []
    for p in core_pages(db):
        seen = last.get(p["url"])
        if seen and at - seen < timedelta(days=cfg.audit.cooldown_days):
            continue
        due.append((seen is not None, seen or at, p["kind"] != "home", p["url"], p))
    return min(due, key=lambda d: d[:4])[4] if due else None


def _missing_terms(text: str, query: str) -> bool:
    want = set(tokens(query))
    return bool(want) and not want <= set(tokens(text or ""))


def findings(cfg: Config, page: dict[str, Any], queries: list[Query], primary: Query | None,
             competitor_words: int = 0, conversions: float | None = None, clicks: int = 0) -> list[dict[str, str]]:
    """Fixed checks, no model. `conversions` is None when conversions aren't measured for this site."""
    out: list[dict[str, str]] = []

    def add(check: str, detail: str) -> None:
        out.append({"check": check, "detail": detail})

    target = primary.query if primary else ""
    title, desc, h1 = page.get("title") or "", page.get("description") or "", page.get("h1") or ""
    if not title:
        add("title_missing", "The page has no <title>.")
    else:
        if not TITLE_RANGE[0] <= len(title) <= TITLE_RANGE[1]:
            add("title_length", f"Title is {len(title)} characters; {TITLE_RANGE[0]}-{TITLE_RANGE[1]} shows in full.")
        if target and _missing_terms(title, target):
            add("title_query", f'Title "{title}" does not contain the main search "{target}".')
    if not desc:
        add("description_missing", "No meta description, so Google writes its own snippet.")
    else:
        if not DESCRIPTION_RANGE[0] <= len(desc) <= DESCRIPTION_RANGE[1]:
            add("description_length", f"Description is {len(desc)} characters; aim for 120-155.")
        if target and _missing_terms(desc, target):
            add("description_query", f'Description does not mention "{target}".')
    h1_count = page.get("h1_count", 1 if h1 else 0)
    if not h1_count:
        add("h1_missing", "The page has no H1.")
    elif h1_count > 1:
        add("h1_multiple", f"The page has {h1_count} H1s; keep one.")
    if h1 and target and _missing_terms(h1, target):
        add("h1_query", f'H1 "{h1}" does not contain the main search "{target}".')
    words = int(page.get("words") or 0)
    floor = max(300, competitor_words // 2)
    if words < floor:
        extra = f" (top results average {competitor_words})" if competitor_words else ""
        add("thin", f"{words} words of main text; under {floor}{extra}.")
    low = [q for q in queries if q.position <= 10 and q.impressions >= cfg.audit.min_impressions
           and q.ctr < cfg.refresh.ctr_ratio * expected_ctr(q.position)]
    if low:
        add("low_ctr", "Seen but rarely clicked: " + "; ".join(
            f'"{q.query}" pos {q.position:.1f}, CTR {q.ctr:.1%} vs about {expected_ctr(q.position):.0%} usual'
            for q in low[:5]))
    near = [q for q in queries if cfg.refresh.min_position <= q.position <= cfg.refresh.max_position
            and q.impressions >= cfg.audit.min_impressions]
    if near:
        add("striking_distance", "Close to page one or the top 3: " + "; ".join(
            f'"{q.query}" pos {q.position:.1f}, {q.impressions} impressions' for q in near[:6]))
    if conversions is not None and clicks >= 20 and conversions == 0:
        add("no_conversions", f"{clicks} search clicks and no tracked conversions; the next step may be unclear.")
    return out


def search_rows(cfg: Config, client: httpx.Client) -> QueryRows:
    """Search Console query rows for one page, or none when Search Console isn't set up."""
    if not (cfg.topics.gsc_enabled and cfg.topics.gsc_site):
        return lambda url: []
    from .topics import gsc_query, gsc_token

    token = gsc_token(cfg)

    def rows(url: str) -> list[dict[str, Any]]:
        return gsc_query(client, token, cfg.topics.gsc_site, cfg.audit.gsc_days, ["query"], row_limit=500,
                         filters=[{"dimension": "page", "operator": "equals", "expression": url}])

    return rows


def live_page(client: httpx.Client, url: str, stored: dict[str, Any], money_pages: list[str]) -> tuple[dict[str, Any], list]:
    """The page as served now (falls back to the crawled copy) with its H1 count and h2/h3 headings."""
    body = fetch_html(client, url)
    if body:
        body = BREAK.sub(" ", body)  # "Building Leaders<br>On and Off" is two words, not "LeadersOn"
    if not body:
        page = dict(stored)
        page["words"] = word_count(page.get("text") or "")
        return page, []
    page = extract_page(url, body, money_pages)
    try:
        page["h1_count"] = len(lxml_html.fromstring(body).xpath("//h1"))
    except (etree.ParserError, ValueError):
        page["h1_count"] = 1 if page["h1"] else 0
    page["words"] = word_count(page["text"])  # main text only, the way a reader counts it
    page["facts"] = visible_text(body)
    return page, headings(body)


def visible_text(body: str) -> str:
    """Every visible line of the body. Main-text extraction drops short blocks such as stat counters
    ("290.5 / CA State Record Points"), and the model must see those facts to use or check them."""
    try:
        doc = lxml_html.fromstring(body)
    except (etree.ParserError, ValueError):
        return ""
    for el in doc.xpath("//script|//style|//noscript|//svg|//template|//head"):
        el.drop_tree()
    lines, seen = [], set()
    for el in doc.iter():
        for piece in (el.text, el.tail):
            line = " ".join((piece or "").split())
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
    return "\n".join(lines)


def fit_title(title: str, limit: int = TITLE_RANGE[1]) -> str:
    """Shorten a title by dropping trailing ' | ' parts first, then words."""
    title = " ".join(title.split())
    while len(title) > limit:
        cut = max(title.rfind(s) for s in SEPARATORS)
        if cut <= 0:
            return clamp_text(title, limit)
        title = title[:cut].rstrip()
    return title


def unsupported_numbers(proposal: dict[str, Any], page_text: str) -> list[str]:
    """Numbers the proposal uses that the page never states: likely invented facts."""
    stated = {n.replace(",", "") for n in NUMBER.findall(page_text)}
    parts = [("title", proposal.get("title", "")), ("description", proposal.get("description", "")),
             ("h1", proposal.get("h1", ""))]
    parts += [(f"edit {i}", e.get("change", "")) for i, e in enumerate(proposal.get("edits", []), 1)]
    warnings = []
    for where, text in parts:
        if where.startswith("edit"):
            text = INSTRUCTION_COUNT.sub(" ", text or "")
        for n in NUMBER.findall(text or ""):
            if n.replace(",", "") not in stated:
                warnings.append(f'{where} uses "{n}", which the page does not state. Check it before using it.')
    return warnings


def clean_proposal(raw: Any, page_text: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    edits = [e for e in raw.get("edits") or [] if isinstance(e, dict) and e.get("change")][:6]
    proposal = {
        "title": fit_title(str(raw.get("title") or "")),
        "description": clamp_text(str(raw.get("description") or ""), DESCRIPTION_RANGE[1]),
        "h1": " ".join(str(raw.get("h1") or "").split()),
        "edits": [{"section": str(e.get("section", "")), "change": str(e["change"]), "why": str(e.get("why", ""))}
                  for e in edits],
        "needs_owner": [str(q) for q in raw.get("needs_owner") or [] if str(q).strip()][:6],
    }
    proposal["warnings"] = unsupported_numbers(proposal, page_text)
    return proposal


def _page_conversions(cfg: Config, db: DB, url: str) -> float | None:
    if not (cfg.measure.posthog_project_id or cfg.measure.conversions_csv):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=cfg.audit.gsc_days)).date().isoformat()
    path = urlparse(url).path.rstrip("/") or "/"
    return sum(r["count"] for r in db.conversions(since)
               if (urlparse(r["url"]).path.rstrip("/") or "/") == path and ":" not in (r.get("source") or ""))


def _since(db: DB, url: str, queries: list[Query], query: str, before: int | None) -> dict[str, Any] | None:
    """How the earlier audit's main search moved: [then, now] for clicks, impressions and position."""
    earlier = [a for a in db.audits(url=url) if before is None or a["id"] != before]
    if not earlier:
        return None
    applied = [a for a in earlier if a["status"] == "applied"]
    prev = max(applied or earlier, key=lambda a: a["id"])
    base = json.loads(prev.get("report") or "{}").get("baseline") or {}
    q = base.get("query") or prev.get("primary_query") or query
    cur = next((x for x in queries if x.query == q), None)
    return {
        "audit_id": prev["id"], "status": prev["status"], "query": q,
        "applied_at": prev.get("resolved_at") or "",
        "clicks": [base.get("clicks", 0), cur.clicks if cur else 0],
        "impressions": [base.get("impressions", 0), cur.impressions if cur else 0],
        "position": [base.get("position"), cur.position if cur else None],
    }


def _fmt_pos(p: Any) -> str:
    return f"{p:.1f}" if isinstance(p, (int, float)) else "-"


def since_line(since: dict[str, Any]) -> str:
    c, i, p = since["clicks"], since["impressions"], since["position"]
    return (f'Since audit #{since["audit_id"]} ({since["status"]}): "{since["query"]}" clicks {c[0]} -> {c[1]}, '
            f"impressions {i[0]} -> {i[1]}, position {_fmt_pos(p[0])} -> {_fmt_pos(p[1])}")


def report_path(cfg: Config, audit_id: int, url: str) -> Path:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "home"
    return cfg.output_dir / "audits" / f"audit-{audit_id}-{slug}.md"


def write_report(cfg: Config, a: dict[str, Any], page: dict[str, Any], queries: list[Query],
                 found: list[dict[str, str]], proposal: dict[str, Any], since: dict[str, Any] | None) -> Path:
    lines = [f"# Core-page audit #{a['id']}: {a['url']}", "",
             f"Kind: {a['kind']}. Main search: \"{a['primary_query'] or '(none)'}\". Created {a['created_at']}.", ""]
    if since:
        lines += ["## Since the last audit", "", since_line(since), ""]
    lines += ["## Searches this page shows up for", ""]
    if queries:
        lines += ["| Query | Clicks | Impressions | CTR | Position |", "|---|---|---|---|---|"]
        lines += [f"| {q.query} | {q.clicks} | {q.impressions} | {q.ctr:.1%} | {q.position:.1f} |"
                  for q in queries[:15]]
    else:
        lines.append("No Search Console data for this page (not connected, or no impressions yet).")
    lines += ["", "## Problems found", ""]
    lines += [f"- **{f['check']}**: {f['detail']}" for f in found] or ["- None of the fixed checks fired."]
    lines += ["", "## Proposed snippet", "", "| | Now | Proposed |", "|---|---|---|"]
    for key, label in (("title", "Title"), ("description", "Description"), ("h1", "H1")):
        lines.append(f"| {label} | {page.get(key) or '(none)'} | {proposal.get(key) or '(no change)'} |")
    lines += ["", "## Edits", ""]
    lines += [f"{i}. **{e['section'] or 'Page'}**: {e['change']}" + (f" _(Why: {e['why']})_" if e["why"] else "")
              for i, e in enumerate(proposal["edits"], 1)] or ["No edits proposed."]
    if proposal["needs_owner"]:
        lines += ["", "## Questions for the owner", ""] + [f"- {q}" for q in proposal["needs_owner"]]
    if proposal["warnings"]:
        lines += ["", "## Check before using", ""] + [f"- {w}" for w in proposal["warnings"]]
    lines += ["", "## When the edits are live", "",
              f"1. Run `autopilot audits done {a['id']}` (pings IndexNow when it is enabled).",
              f"2. Request indexing in Search Console: URL Inspection, paste {a['url']}, then \"Request indexing\".",
              "3. The next audit of this page (after the cooldown) reports what changed.", ""]
    path = report_path(cfg, a["id"], a["url"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def run_audit(cfg: Config, db: DB, client: httpx.Client, llm: LLM, url: str | None = None, log: Log = print,
              at: datetime | None = None, query_rows: QueryRows | None = None) -> int | None:
    """Audit `url` (or the next due core page). Returns the audit id, or None when no page is due."""
    if url:
        stored = next((p for p in db.pages() if p["url"].rstrip("/") == url.rstrip("/")), None)
        if not stored:
            raise ValueError(f"{url} is not in the crawled pages. Run `autopilot crawl` first.")
    else:
        stored = pick_page(cfg, db, at)
        if not stored:
            log("Core-page audit: every home and money page was audited in the last "
                f"{cfg.audit.cooldown_days} days.")
            return None
    url = stored["url"]
    brand = db.get(BRAND_KEY) or {}
    business = brand.get("business_name") or cfg.site.name or cfg.site.url
    page, page_headings = live_page(client, url, stored, cfg.site.money_pages)
    page["kind"] = stored.get("kind") or page.get("kind")
    queries = to_queries((query_rows or search_rows(cfg, client))(url))[: cfg.audit.max_queries]
    primary = primary_query(queries, [brand.get("business_name", ""), cfg.site.name])
    target = primary.query if primary else (page.get("h1") or page.get("title") or business)
    res = research(cfg, client, target)
    clicks = sum(q.clicks for q in queries)
    found = findings(cfg, page, queries, primary, res.avg_words, _page_conversions(cfg, db, url), clicks)

    def fmt_competitor(c: Any) -> str:
        heads = "; ".join(t for _, t in c.headings[:8])
        return f"- {c.title} ({c.words} words): {heads or c.snippet}"

    prompt = PROMPT.format(
        url=url, kind=page["kind"], primary=target, days=cfg.audit.gsc_days,
        queries="\n".join(f"{q.query} | {q.clicks} | {q.impressions} | {q.ctr:.1%} | {q.position:.1f}"
                          for q in queries) or "(no Search Console data)",
        findings="\n".join(f"- {f['detail']}" for f in found) or "- none",
        title=page.get("title") or "(none)", description=page.get("description") or "(none)",
        h1=page.get("h1") or "(none)", headings="; ".join(t for _, t in page_headings[:20]) or "(none)",
        competitors="\n".join(fmt_competitor(c) for c in res.competitors) or "(no search results available)",
        questions="; ".join(res.questions[:8]) or "(none)",
        text=clamp_text(page.get("facts") or page.get("text") or "", 8000),
    )
    system = SYSTEM.format(business=business, language=cfg.site.language)
    page_text = " ".join([page.get("facts") or "", page.get("text") or "", page.get("title") or "", page.get("description") or "",
                          page.get("h1") or "", stored.get("text") or ""])
    proposal = clean_proposal(complete_json(llm, system, prompt), page_text)
    baseline = {"query": primary.query, "clicks": primary.clicks, "impressions": primary.impressions,
                "position": primary.position} if primary else {}
    created = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    audit_id = db.insert_audit({
        "url": url, "kind": page["kind"], "primary_query": primary.query if primary else "",
        "queries": json.dumps([asdict(q) for q in queries]), "findings": json.dumps(found),
        "proposal": json.dumps(proposal), "status": "open", "created_at": created,
    })
    since = _since(db, url, queries, primary.query if primary else "", audit_id)
    a = db.audit(audit_id)
    path = write_report(cfg, a, page, queries, found, proposal, since)
    db.update_audit(audit_id, report=json.dumps({"baseline": baseline, "since": since}), report_path=str(path))
    log(f"Core-page audit #{audit_id}: {url} (main search \"{target}\"), {len(found)} problems, "
        f"{len(proposal['edits'])} edits proposed. Report: {path}")
    if since:
        log(since_line(since))
    if proposal["warnings"]:
        log(f"  {len(proposal['warnings'])} proposed numbers are not on the page; the report lists them.")
    return audit_id


def audit_due(db: DB, at: datetime | None = None, hours: float = 20) -> bool:
    last = db.get(LAST_KEY)
    if not last:
        return True
    t = datetime.fromisoformat(last)
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return (at or datetime.now(timezone.utc)) - t >= timedelta(hours=hours)


def daily_audit(cfg: Config, db: DB, client: httpx.Client, llm: LLM, log: Log = print,
                query_rows: QueryRows | None = None) -> int | None:
    """One audit per day inside `autopilot run`. A failure is logged and never stops the run."""
    if not cfg.audit.enabled or not core_pages(db) or not audit_due(db):
        return None
    db.set(LAST_KEY, now())  # set first, so a page that keeps failing doesn't retry every run
    try:
        return run_audit(cfg, db, client, llm, log=log, query_rows=query_rows)
    except Exception as exc:
        log(f"Core-page audit failed: {exc!r}")
        return None


def mark_done(cfg: Config, db: DB, client: httpx.Client, audit_id: int, note: str = "", log: Log = print) -> None:
    from .pipeline import ping_indexnow  # pipeline imports this module

    a = db.audit(audit_id)
    if not a:
        raise ValueError(f"No audit #{audit_id}")
    db.update_audit(audit_id, status="applied", resolved_at=now(), note=note)
    log(f"Audit #{audit_id} marked applied.")
    if ping_indexnow(cfg, db, client, [a["url"]], log):
        log("IndexNow pinged (Bing, Yandex and others; Google does not use IndexNow).")
    log(f"Request indexing in Search Console: URL Inspection, paste {a['url']}, then \"Request indexing\".")


def dismiss(db: DB, audit_id: int, note: str = "") -> None:
    if not db.audit(audit_id):
        raise ValueError(f"No audit #{audit_id}")
    db.update_audit(audit_id, status="dismissed", resolved_at=now(), note=note)
