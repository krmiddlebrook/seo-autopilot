"""0-100 optimization score from explicit, explainable on-page checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .links import host_allowed
from .textutil import strip_markdown, tokens, word_count


@dataclass
class Check:
    name: str
    passed: bool
    weight: int
    detail: str = ""


@dataclass
class ScoreReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def score(self) -> int:
        total = sum(c.weight for c in self.checks) or 1
        return round(100 * sum(c.weight for c in self.checks if c.passed) / total)

    def failing(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "checks": [c.__dict__ for c in self.checks]}

    def summary(self) -> str:
        return "\n".join(f"{'✓' if c.passed else '✗'} {c.name} ({c.weight}) {c.detail}" for c in self.checks)


def contains_keyword(text: str, keyword: str, ratio: float = 1.0) -> bool:
    """Exact phrase, or (for long-tail queries) most of the keyword's tokens in any order."""
    if keyword.lower() in text.lower():
        return True
    kw = set(tokens(keyword))
    if not kw:
        return False
    return len(kw & set(tokens(text))) / len(kw) >= ratio


_A = "['’]"
# First-person anecdotes, made-up clients and testimonials: the model has no first-hand experience to report.
EXPERIENCE = re.compile("|".join([
    rf"\bI(?:{_A}ve| have| had|{_A}d)\s+(?:\w+\s+)?(?:watched|seen|coached|trained|worked with|helped|noticed|played|met)\b",
    r"\bI (?:once|remember|recall)\b",
    r"\bwhen I (?:was|coached|played|trained|worked|started)\b",
    r"\bin my (?:experience|\d+ years|years|career|time (?:as|at|coaching|playing))\b",
    r"\bmy (?:own )?(?:clients|players|athletes|lads|students|patients|customers)\b",
    r"\bone of (?:my|our) (?:clients|players|athletes|customers|members)\b",
    r"\bour (?:clients|customers|players|athletes|members|users) (?:have|tell|told|report|say|said|often|regularly)\b",
    rf"\bwe(?:{_A}ve| have)\s+(?:\w+\s+)?(?:coached|trained|helped|seen|watched|worked with)\b",
]), re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+\.|>)\s+")


def _prose_lines(markdown: str):
    for line in markdown.split("\n"):
        prose = not line.lstrip().startswith(("#", "|"))
        prefix = m.group(0) if prose and (m := LIST_PREFIX.match(line)) else ""
        yield line, prose, prefix


def invented_experience(markdown: str) -> list[str]:
    """Sentences that claim first-hand experience (headings are skipped, so FAQ questions don't count)."""
    hits = []
    for line, prose, prefix in _prose_lines(markdown):
        if prose and EXPERIENCE.search(line):
            hits += [strip_markdown(x).strip() for x in SENTENCE.split(line[len(prefix):]) if EXPERIENCE.search(x)]
    return hits


def strip_invented_experience(markdown: str) -> str:
    """Drop the flagged sentences, and any line left empty."""
    out = []
    for line, prose, prefix in _prose_lines(markdown):
        if not prose or not EXPERIENCE.search(line):
            out.append(line)
            continue
        kept = [x for x in SENTENCE.split(line[len(prefix):]) if not EXPERIENCE.search(x)]
        if kept:
            out.append(prefix + " ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# -- claims about the business ------------------------------------------------------------------------------------

WE = re.compile(r"\b(?:we|we're|we've|we'll|our|ours)\b", re.I)
# a sentence that follows a brand sentence and keeps talking about the brand
CARRYOVER = re.compile(r"^(?:it|its|they|their|this (?:club|program|team|academy|school|studio|gym|company))\b", re.I)
# business facts the model likes to make up; checked in any paragraph that names the business
BUSINESS_FACT = re.compile(
    r"\b(?:regist\w*|enrol\w*|sign[- ]?ups?|tuition|fees?|pric\w*|costs?|scholarships?|financial aid|discounts?|"
    r"spots?|waitlist|trial|try (?:a|an|one) (?:session|class|practice)|observe|schedules?|seasons?|seasonal\w*|curriculum|"
    r"founded|since \d{4}|certified|accredited|guarantee\w*|membership|ages? \d+)\b", re.I)
NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
# words that say nothing checkable about the business, including the "check with them" phrasing the writer is told to use
GENERIC = set("""
    also many often help helps make more most like well just very each every other some such only your about have been
    being would could should into over these those which while where there here really always usually typically great
    good best right first next year years time times youre theyre dont contact call email visit check confirm current
    details detail information option options question questions reach learn find latest directly today website site page
    team ask asking sure know want need able offer offers provide provides available include includes including family
    families parent parents child children kids local area program programs""".split())
STEM = 6


def _stem(word: str) -> str:
    return word[:STEM]


def _claim_words(sentence: str, ignore: set[str]) -> list[str]:
    return [t for t in tokens(sentence) if len(t) > 3 and not t.isdigit() and t not in GENERIC and _stem(t) not in ignore]


def _numbers(text: str) -> set[str]:
    return {n.lstrip("$").replace(",", "").rstrip(".") for n in NUMBER.findall(text)}


def unsupported_brand_claims(markdown: str, names: list[str], source: str) -> list[str]:
    """Sentences about the business (named, "we/our", or a business fact in a paragraph that names it) that the
    crawled site text does not back up: a number it never states, or mostly words it never uses."""
    names = [n.strip() for n in names if n and len(n.strip()) > 2]
    if not names:
        return []
    name_re = re.compile("|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True)), re.I)
    ignore = {_stem(t) for n in names for t in tokens(n)}
    known = {_stem(t) for t in tokens(source)}
    known_numbers = _numbers(source)
    hits = []
    for line, prose, prefix in _prose_lines(markdown):
        if not prose or not line.strip():
            continue
        sentences = [strip_markdown(x).strip() for x in SENTENCE.split(line[len(prefix):]) if x.strip()]
        paragraph_names_brand = any(name_re.search(x) for x in sentences)
        previous_about_brand = False
        for sent in sentences:
            about = bool(name_re.search(sent) or WE.search(sent)) or (previous_about_brand and CARRYOVER.match(sent)) \
                or (paragraph_names_brand and BUSINESS_FACT.search(sent))
            previous_about_brand = bool(about)
            if not about:
                continue
            if _numbers(sent) - known_numbers:
                hits.append(sent)
                continue
            words = _claim_words(sent, ignore)
            missing = [w for w in words if _stem(w) not in known]
            if len(missing) >= 2 and len(missing) / len(words) >= 0.4:
                hits.append(sent)
    return hits


DOMAIN = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|io|co|us|info|biz|uk|ca|au|nz|ie))\b", re.I)


def unverified_domains(markdown: str, allowed: set[str]) -> list[str]:
    """Website addresses in the article (linked or bare) that neither research nor the site itself returned."""
    found = dict.fromkeys(m.group(1).lower().removeprefix("www.") for m in DOMAIN.finditer(markdown))
    return [d for d in found if not host_allowed(d, allowed)]


def score_article(
    *, keyword: str, h1: str, meta_title: str, meta_description: str, slug: str, markdown: str,
    internal_links: int, external_links: int, faq_count: int, has_image: bool,
    min_words: int, want_internal: int, want_external: int, want_faq: bool,
    seo_title: bool = True, image_expected: bool = True,
    brand_claims: list[str] | None = None, unverified: list[str] | None = None,
) -> ScoreReport:
    """seo_title=False: the site uses the H1 as <title>, so the H1 is scored in place of the meta title.
    image_expected=False: the site makes its own image, so a missing one costs nothing.
    brand_claims / unverified: from `unsupported_brand_claims` and `unverified_domains` (the caller has the site text
    and the research hosts); None skips the check."""
    text = strip_markdown(markdown)
    words = word_count(text)
    first_100 = " ".join(text.split()[:100])
    h2s = re.findall(r"^##\s+(.+)$", markdown, flags=re.M)
    slug_tokens = set(tokens(slug.replace("-", " ")))
    kw_tokens = set(tokens(keyword))
    long_paras = [p for p in markdown.split("\n\n") if not p.startswith(("#", "|", "-", "*", "1.")) and word_count(p) > 150]

    invented = invented_experience(markdown)

    checks = [Check("keyword_in_h1", contains_keyword(h1, keyword, 0.75), 10, h1)]
    if seo_title:
        checks += [
            Check("keyword_in_meta_title", contains_keyword(meta_title, keyword, 0.75), 10, meta_title),
            Check("meta_title_differs_from_h1", meta_title.strip().lower() != h1.strip().lower(), 8),
            Check("meta_title_length", 30 <= len(meta_title) <= 60, 5, f"{len(meta_title)} chars"),
        ]
    else:
        checks.append(Check("h1_length", 30 <= len(h1) <= 60, 8, f"{len(h1)} chars (used as <title>)"))
    checks += [
        Check("keyword_in_intro", contains_keyword(first_100, keyword, 0.75), 10, "first 100 words"),
        Check("keyword_in_slug", bool(kw_tokens) and len(kw_tokens & slug_tokens) / len(kw_tokens) >= 0.6, 5, slug),
        Check("keyword_in_meta_description", contains_keyword(meta_description, keyword, 0.75), 5),
        Check("no_invented_experience", not invented, 10, " | ".join(invented)[:300]),
        *([Check("no_unsupported_brand_claims", not brand_claims, 10, " | ".join(brand_claims)[:300])]
          if brand_claims is not None else []),
        *([Check("no_unverified_domains", not unverified, 4, ", ".join(unverified)[:300])] if unverified is not None else []),
        Check("meta_description_length", 110 <= len(meta_description) <= 160, 5, f"{len(meta_description)} chars"),
        Check("length", words >= min_words, 12, f"{words} words (min {min_words})"),
        Check("structure", len(h2s) >= 4 and len(set(h.lower() for h in h2s)) == len(h2s), 6, f"{len(h2s)} H2s"),
        Check("internal_links", internal_links >= min(want_internal, 3), 8, f"{internal_links} internal"),
        Check("external_links", external_links >= min(want_external, 1), 4, f"{external_links} external"),
        Check("faq", faq_count >= 3 or not want_faq, 6, f"{faq_count} questions"),
        *([Check("featured_image", has_image, 3)] if image_expected else []),
        Check("readable_paragraphs", not long_paras, 3, f"{len(long_paras)} paragraphs over 150 words"),
    ]
    return ScoreReport(checks)
