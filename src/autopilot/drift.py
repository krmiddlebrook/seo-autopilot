"""Keep published facts true: compare money pages between crawls and flag every article that quotes a fact the site
no longer states.

A fact is a sentence on a money page that carries a checkable value: a price, a number, a time, a weekday or a month
("Junior Squad is $375 per season", "Practice runs Tuesdays at 6pm"). When the next crawl shows that value changed or
vanished, every stored article whose own sentence repeats the old value in the same context gets a row in
`fact_issues`, and unpublished drafts are held for review so the stale fact never goes out. `autopilot facts fix`
rewrites the quoted sentence from the current page.

Every comparison reports its own counts (money pages before and after, pages skipped and why, articles scanned), so
"no changes" is never the silent result of comparing nothing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .db import DB, now
from .llm import LLM, complete_text
from .render import to_html
from .score import SENTENCE
from .textutil import strip_markdown, tokens

LAST_KEY = "drift:last"
FACT_KINDS = ("money",)

WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
VALUE = re.compile(
    rf"\$\s?\d[\d,]*(?:\.\d+)?|"  # $375, $1,200.00
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)|"  # 6pm, 6:30 pm
    r"\b\d[\d,]*(?:\.\d+)?%?|"  # 12, 4.5, 20%
    rf"\b(?:{WEEKDAYS})s?\b|\b(?:{MONTHS})\b",
    re.I,
)
VALUE_WORDS = set(WEEKDAYS.split("|")) | set(MONTHS.split("|")) | {"am", "pm"}


def norm_value(v: str) -> str:
    v = v.lower().replace(" ", "").replace(",", "").replace(".m.", "m")
    if re.fullmatch(rf"(?:{WEEKDAYS})s", v):
        v = v[:-1]
    return v


def values(text: str) -> set[str]:
    return {norm_value(m.group(0)) for m in VALUE.finditer(text)}


def strong(value: str) -> bool:
    """Specific enough that one shared context word is enough to call it a quote: money, times, 3+ digit numbers."""
    return value.startswith("$") or value.endswith(("am", "pm", "%")) or len(re.sub(r"\D", "", value)) >= 3


def context(text: str) -> set[str]:
    return {t for t in tokens(text) if not t.isdigit() and t not in VALUE_WORDS and len(t) > 2}


def sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*+> ").strip()
        if line and not line.startswith(("#", "|")):
            out += [s.strip() for s in SENTENCE.split(line) if s.strip()]
    return out


def fact_sentences(text: str) -> list[str]:
    return [s for s in sentences(text) if values(s) and 3 <= len(s.split()) <= 80]


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Change:
    page_url: str
    kind: str  # changed | removed | page_gone
    old_fact: str
    new_fact: str
    stale: list[str]  # values the page no longer states
    fresh: list[str]  # the values that replaced them, when the sentence was edited in place


def diff_page(url: str, old_text: str, new_text: str) -> list[Change]:
    """Facts on the old page whose values the new page no longer states in that context."""
    new_facts = [(s, values(s), context(s)) for s in fact_sentences(new_text)]
    page_values = values(new_text)
    out = []
    for old in fact_sentences(old_text):
        old_vals, old_ctx = values(old), context(old)
        best, best_score = None, 0.0
        for s, vals, ctx in new_facts:
            score = overlap(old_ctx, ctx)
            if score > best_score:
                best, best_score = (s, vals), score
        if best and best_score >= 0.5:
            stale, fresh = old_vals - best[1], best[1] - old_vals
            if stale:
                out.append(Change(url, "changed", old, best[0], sorted(stale), sorted(fresh)))
            continue
        stale = old_vals - page_values  # the sentence was rewritten: only values gone from the whole page count
        if stale:
            hint = best[0] if best and best_score >= 0.3 else ""
            out.append(Change(url, "changed" if hint else "removed", old, hint, sorted(stale),
                              sorted(best[1] - old_vals) if hint else []))
    return out


def quotes(markdown: str, change: Change, links_page: bool = False) -> list[str]:
    """Sentences in an article that repeat a stale value in the old fact's context."""
    stale, ctx = set(change.stale), context(change.old_fact)
    hits = []
    for s in sentences(strip_markdown(markdown)):
        hit = values(s) & stale
        if not hit:
            continue
        shared = len(context(s) & ctx)
        need = 1 if links_page or any(strong(v) for v in hit) else 2
        if shared >= need:
            hits.append(s)
    return hits


# -- running a comparison ----------------------------------------------------------------------------------------

@dataclass
class DriftReport:
    at: str = field(default_factory=now)
    money_before: int = 0
    compared: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)  # {url, reason}
    changes: list[Change] = field(default_factory=list)
    articles_scanned: int = 0
    flagged: list[int] = field(default_factory=list)  # new fact_issue ids
    held: list[int] = field(default_factory=list)  # article ids moved back to review
    missing: list[str] = field(default_factory=list)  # data sources that would unlock more

    @property
    def complete(self) -> bool:
        return self.compared + len(self.skipped) == self.money_before

    def summary(self) -> str:
        return (f"Fact check: compared {self.compared}/{self.money_before} money page(s)"
                + (f" ({len(self.skipped)} skipped)" if self.skipped else "")
                + f", {len(self.changes)} fact change(s), scanned {self.articles_scanned} article(s), "
                  f"{len(self.flagged)} new issue(s)" + (f", {len(self.held)} draft(s) held" if self.held else ""))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["summary"] = self.summary()
        return d


def check_articles(db: DB, changes: list[Change], rep: DriftReport) -> None:
    arts = [a for a in db.articles() if a["status"] not in ("rejected", "failed") and a.get("markdown")]
    rep.articles_scanned = len(arts)
    for a in arts:
        held = False
        for ch in changes:
            for quote in quotes(a["markdown"], ch, links_page=ch.page_url.rstrip("/") in a["markdown"]):
                issue_id = db.add_fact_issue(a["id"], ch.page_url, ch.kind, ch.old_fact, ch.new_fact, ch.stale,
                                             ch.fresh, quote)
                if issue_id:
                    rep.flagged.append(issue_id)
                    if a["status"] == "approved" and not held:  # never publish a draft that quotes a stale fact
                        db.update_article(a["id"], status="pending_review")
                        rep.held.append(a["id"])
                        held = True


def compare(db: DB, old_pages: dict[str, dict[str, Any]], new_pages: list[dict[str, Any]],
            gone: Callable[[str], bool] = lambda url: False) -> DriftReport:
    """Diff money pages from the last crawl (`old_pages`, keyed by URL) against this one, then flag articles.

    `gone(url)` says whether a money page missing from this crawl is confirmed gone (404/410), as opposed to merely
    not fetched this time; only confirmed pages count as removed programs."""
    rep = DriftReport()
    old_money = {u: p for u, p in old_pages.items() if p.get("kind") in FACT_KINDS}
    rep.money_before = len(old_money)
    new_by_url = {p["url"]: p for p in new_pages}
    if not old_money:
        rep.missing.append("money pages from an earlier crawl: the first crawl is the baseline that later crawls are "
                           "compared with" if not old_pages else
                           "money pages: none were found, so no prices or schedules are tracked; list them in "
                           "site.money_pages")
    for url, old in old_money.items():
        new = new_by_url.get(url)
        old_text = old.get("text") or ""
        if new is None:
            if gone(url):
                rep.compared += 1
                for fact in fact_sentences(old_text):
                    rep.changes.append(Change(url, "page_gone", fact, "", sorted(values(fact)), []))
            else:
                rep.skipped.append({"url": url, "reason": "not fetched this crawl and not confirmed gone"})
            continue
        new_text = new.get("text") or ""
        if len(new_text.split()) < 0.3 * len(old_text.split()):
            rep.skipped.append({"url": url, "reason": f"main text fell from {len(old_text.split())} to "
                                                      f"{len(new_text.split())} words; treated as a rendering "
                                                      "failure, not a fact change (see `autopilot health`)"})
            continue
        rep.compared += 1
        rep.changes += diff_page(url, old_text, new_text)
    if rep.changes:
        check_articles(db, rep.changes, rep)
    else:
        rep.articles_scanned = len([a for a in db.articles() if a["status"] not in ("rejected", "failed")])
    db.set(LAST_KEY, rep.to_dict())
    return rep


# -- fixing -----------------------------------------------------------------------------------------------------

def _raw_sentence(markdown: str, quote: str) -> str:
    """The sentence in the raw markdown (links and emphasis intact) whose plain text is `quote`."""
    for line in markdown.splitlines():
        body = line.strip().lstrip("-*+> ").strip()
        for s in SENTENCE.split(body):
            if strip_markdown(s).strip() == quote:
                return s.strip()
    return ""


def _swap(raw: str, stale: list[str], fresh: list[str]) -> str:
    """Replace each stale value with its replacement, when the change was a clean one-for-one edit."""
    if len(stale) != 1 or len(fresh) != 1:
        return ""
    out, n = raw, 0
    for m in reversed(list(VALUE.finditer(raw))):
        if norm_value(m.group(0)) == stale[0]:
            new = fresh[0]
            if m.group(0).startswith("$ "):
                new = "$ " + new[1:]
            elif re.search(r"\d,\d", m.group(0)) and new.lstrip("$").isdigit():
                new = (new[0] if new.startswith("$") else "") + f"{int(new.lstrip('$')):,}"
            out = out[:m.start()] + new + out[m.end():]
            n += 1
    return out if n else ""


REWRITE_SYSTEM = ("You fix one sentence in a published article so it matches the business's own website. Change only "
                  "what the current page contradicts, keep links and formatting, and invent nothing. Reply with the "
                  "corrected sentence only.")


def corrected(raw: str, issue: dict[str, Any], page_text: str, llm: LLM | None) -> str:
    """The replacement sentence: "" means drop it (the fact no longer exists and nothing safe replaces it)."""
    stale, fresh = issue["old_values"], issue["new_values"]
    swapped = _swap(raw, stale, fresh)
    if swapped:
        return swapped
    if llm and issue["new_fact"]:
        text = complete_text(llm, REWRITE_SYSTEM,
                             f"Sentence: {raw}\n\nThe site used to say: {issue['old_fact']}\n"
                             f"The site now says: {issue['new_fact']}\n\nCorrected sentence:", max_tokens=300)
        text = text.strip().splitlines()[0].strip() if text.strip() else ""
        # accept it only if it drops every stale value and every value it states is on the current page
        if text and not (values(text) & set(stale)) and values(text) <= values(page_text) | values(raw) - set(stale):
            return text
    return ""


def apply_fix(markdown: str, raw: str, replacement: str) -> str:
    if replacement:
        return markdown.replace(raw, replacement, 1)
    out = markdown.replace(raw, "", 1)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out)
