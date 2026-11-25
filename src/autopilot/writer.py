"""Outline and drafting. Drafting runs one call per section so small local models stay coherent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .brand import brand_brief
from .config import Config
from .llm import LLM, complete_json, complete_text
from .research import Research
from .textutil import clamp_text, slugify, tokens, word_count

SYSTEM = (
    "You are a senior content writer for {business}. You write useful, specific, human articles that rank. "
    "Write in {language}. No filler, no hype, no clichés like 'in today's fast-paced world', 'delve', 'unlock', "
    "'game-changer', 'elevate' or 'navigate the landscape'. Prefer concrete examples, numbers and steps. "
    "Vary sentence length. Never invent statistics, quotes or studies. You have no first-hand experience: never write "
    "first-person anecdotes ('I've watched...', 'in my experience'), made-up clients, case studies or testimonials.\n"
    "Facts about {business} itself (prices, fees, ages, schedules, seasons, programs, memberships, scholarships or "
    "financial aid, registration, trial sessions, staff, results, policies) may ONLY come from the 'Facts from "
    "{business}'s own website' you are given. Use those facts exactly (real prices, not ranges). If a detail is not "
    "there, do not guess or generalize it: tell the reader to check with {business} directly.\n"
    "Only link to URLs you are given. Never mention or link any other website address."
)


@dataclass
class Section:
    h2: str
    h3: list[str] = field(default_factory=list)
    notes: str = ""
    links: list[dict[str, str]] = field(default_factory=list)  # filled by the link planner
    facts: str = ""  # crawled text from the site's own pages relevant to this section (facts.plan_facts)
    markdown: str = ""


@dataclass
class Outline:
    keyword: str
    h1: str
    meta_title: str
    meta_description: str
    slug: str
    excerpt: str
    sections: list[Section]
    faq_questions: list[str] = field(default_factory=list)
    image_prompt: str = ""


@dataclass
class Draft:
    outline: Outline
    intro: str = ""
    conclusion: str = ""
    faq: list[dict[str, str]] = field(default_factory=list)
    external_links: list[dict[str, str]] = field(default_factory=list)
    facts: str = ""  # general fact block (money, about and home pages) for intro, closing and FAQ


def system_prompt(brand: dict[str, Any], cfg: Config) -> str:
    style = f"\n\nHouse style:\n{cfg.writing.style}" if cfg.writing.style else ""
    return SYSTEM.format(business=brand.get("business_name") or cfg.site.name or cfg.site.url, language=cfg.site.language) + \
        "\n\nBrand DNA:\n" + brand_brief(brand) + style


def apply_replacements(draft: Draft, rules: dict[str, str]) -> None:
    """House-style fixes the model can't be trusted with (banned characters, spellings). Repeats until stable."""
    def fix(text: str) -> str:
        for _ in range(10):
            new = text
            for old, rep in rules.items():
                new = new.replace(old, rep)
            if new == text:
                break
            text = new
        return text

    o = draft.outline
    o.h1, o.meta_title, o.meta_description, o.excerpt = fix(o.h1), fix(o.meta_title), fix(o.meta_description), fix(o.excerpt)
    draft.intro, draft.conclusion = fix(draft.intro), fix(draft.conclusion)
    for s in o.sections:
        s.markdown = fix(s.markdown)
    draft.faq = [{"q": fix(f["q"]), "a": fix(f["a"])} for f in draft.faq]


OUTLINE_PROMPT = """Plan an article targeting the search query "{keyword}".

SERP research:
{research}

Requirements:
- Beat the top results: cover what they cover, then add what they miss (specifics, steps, local or practical detail).
- Target length about {target_words} words across 5-8 H2 sections, each with 0-3 H3s.
- Mention {business} naturally where it genuinely helps the reader, not as an ad.
- h1: clear and specific, includes the keyword or a close variant.{h1_rule}
- meta_title: 45-60 characters, includes the keyword, and must be worded DIFFERENTLY from the h1.
- meta_description: 120-155 characters, includes the keyword, gives a reason to click.
- slug: short, lowercase, hyphenated, contains the keyword's main words.
- faq: {faq_count} questions searchers actually ask (prefer the research questions); do not duplicate H2s.
- Do NOT include an introduction, conclusion or FAQ section in "sections"; those are written separately.

Return JSON:
{{"h1": "", "meta_title": "", "meta_description": "", "slug": "", "excerpt": "1-2 sentence summary",
  "sections": [{{"h2": "", "h3": [""], "notes": "what this section must say, key points, examples"}}],
  "faq": [""], "image_prompt": "a concrete visual scene for the featured image, no text in the image"}}"""


def make_outline(llm: LLM, brand: dict[str, Any], cfg: Config, keyword: str, research: Research) -> Outline:
    target = max(cfg.writing.min_words, min(cfg.writing.max_words, int((research.avg_words or cfg.writing.min_words) * 1.1)))
    data = complete_json(llm, system_prompt(brand, cfg), OUTLINE_PROMPT.format(
        keyword=keyword, research=research.brief() or "(no SERP data)", target_words=target,
        business=brand.get("business_name", "the business"), faq_count=cfg.writing.faq_count,
        h1_rule="" if cfg.uses_seo_title else " 60 characters or fewer: the site uses it as the page <title>.",
    ))
    return normalize_outline(data, keyword, brand, cfg, research)


def normalize_outline(data: dict[str, Any], keyword: str, brand: dict[str, Any], cfg: Config, research: Research) -> Outline:
    skip = ("introduction", "conclusion", "faq", "frequently asked", "final thoughts", "wrapping up", "summary")
    sections = []
    for s in data.get("sections") or []:
        if isinstance(s, str):
            s = {"h2": s}
        h2 = str(s.get("h2", "")).strip()
        if not h2 or any(h2.lower().startswith(w) for w in skip):
            continue
        h3 = [str(x).strip() for x in s.get("h3") or [] if str(x).strip()]
        sections.append(Section(h2=h2, h3=h3[:3], notes=str(s.get("notes", ""))))
    if not sections:
        raise RuntimeError("Outline has no usable sections")

    h1 = str(data.get("h1") or keyword.title()).strip()
    meta_title = str(data.get("meta_title") or "").strip()
    business = brand.get("business_name") or cfg.site.name
    if not cfg.uses_seo_title:
        meta_title = h1  # the target ignores a separate SEO title
    elif not meta_title or meta_title.lower() == h1.lower():
        # Soro's reviewed weak spot: an SEO title identical to the H1
        meta_title = f"{keyword.title()}: {business}" if business else f"{keyword.title()} Explained"
    slug = slugify(str(data.get("slug") or keyword)) or slugify(keyword)
    kw_tokens = tokens(keyword)
    if kw_tokens and not any(t in slug for t in kw_tokens):
        slug = slugify(keyword)

    faq = [str(q).strip() for q in data.get("faq") or [] if str(q).strip()]
    if len(faq) < cfg.writing.faq_count:
        faq += [q for q in research.questions if q not in faq]
    h2_lower = {s.h2.lower() for s in sections}
    faq = [q if q.endswith("?") else q + "?" for q in faq if q.lower() not in h2_lower][: cfg.writing.faq_count]

    return Outline(
        keyword=keyword,
        h1=h1,
        meta_title=clamp_text(meta_title, 60),
        meta_description=clamp_text(str(data.get("meta_description") or data.get("excerpt") or ""), 158),
        slug=slug,
        excerpt=clamp_text(str(data.get("excerpt") or data.get("meta_description") or ""), 300),
        sections=sections[:8],
        faq_questions=faq if cfg.writing.faq else [],
        image_prompt=str(data.get("image_prompt") or ""),
    )


def _facts_instruction(facts: str, business: str) -> str:
    if not facts.strip():
        return (f"\nFacts from {business}'s own website: (none available). Do not state any specifics about {business} "
                "(prices, schedules, ages, programs, policies).\n")
    return (f"\nFacts from {business}'s own website (the ONLY source for claims about {business}; quote prices, ages "
            f"and schedules exactly, and say nothing about {business} that is not here):\n{facts}\n")


def _links_instruction(links: list[dict[str, str]]) -> str:
    if not links:
        return ""
    lines = "\n".join(f"- [{l['anchor']}]({l['url']}): {l.get('why', '')}" for l in links)
    return ("\nWeave these links into the prose as natural markdown links with descriptive anchor text "
            f"(use each once, do not list them at the end):\n{lines}\n")


INTRO_PROMPT = """Write the introduction for the article "{h1}" (target query: "{keyword}").
Article plan: {plan}
- 2-3 short paragraphs, 90-160 words total, no heading.
- Use the exact phrase "{keyword}" within the first two sentences.
- Open with the reader's real problem or question, not a generic statement.
- Say what the article covers and why the reader should trust it.
{facts}{links}Return markdown only."""

SECTION_PROMPT = """Article: "{h1}" (target query: "{keyword}").
Full plan: {plan}
Previously written (for continuity, do not repeat it): {previous}

Write ONLY this section, about {words} words:
## {h2}
Subheadings to use (as ###): {h3}
Notes: {notes}
Research hints: {hints}
{facts}{links}
Start with the line "## {h2}". Use short paragraphs, and lists or a table where they genuinely help.
Return markdown only."""

CONCLUSION_PROMPT = """Write a closing section for the article "{h1}" (target query: "{keyword}").
Plan: {plan}
- Start with a "## " heading that is specific to the topic (not "Conclusion").
- 80-140 words: the key takeaway, one concrete next step, then a natural mention of {business} and its CTA: {cta}
{facts}{links}Return markdown only."""

FAQ_PROMPT = """Answer these questions for readers of "{h1}". Each answer is 40-90 words, direct, accurate, and starts
with the answer itself (good for featured snippets). Do not invent statistics.
{facts}Questions:
{questions}
Return JSON: {{"faq": [{{"q": "question", "a": "answer"}}]}}"""


def section_word_budget(outline: Outline, total: int) -> int:
    return max(150, (total - 300) // max(1, len(outline.sections)))


def write_intro(llm: LLM, system: str, outline: Outline, links: list[dict[str, str]], facts: str = "",
                business: str = "the business") -> str:
    return complete_text(llm, system, INTRO_PROMPT.format(
        h1=outline.h1, keyword=outline.keyword, plan=_plan(outline), links=_links_instruction(links),
        facts=_facts_instruction(facts, business)))


def write_section(llm: LLM, system: str, outline: Outline, section: Section, research: Research, words: int, previous: str,
                  business: str = "the business") -> str:
    hints = "; ".join(st for st in research.subtopics if set(tokens(st)) & set(tokens(section.h2 + " " + " ".join(section.h3))))
    md = complete_text(llm, system, SECTION_PROMPT.format(
        h1=outline.h1, keyword=outline.keyword, plan=_plan(outline), previous=previous or "(this is the first section)",
        words=words, h2=section.h2, h3=", ".join(section.h3) or "(none needed)", notes=section.notes or "-",
        hints=hints or "-", links=_links_instruction(section.links), facts=_facts_instruction(section.facts, business),
    ), max_tokens=max(1024, words * 3))
    if not md.lstrip().startswith("## "):
        md = f"## {section.h2}\n\n{md}"
    return md.strip()


def write_conclusion(llm: LLM, system: str, outline: Outline, brand: dict[str, Any], links: list[dict[str, str]],
                     facts: str = "") -> str:
    business = brand.get("business_name") or "us"
    md = complete_text(llm, system, CONCLUSION_PROMPT.format(
        h1=outline.h1, keyword=outline.keyword, plan=_plan(outline), business=business,
        cta=brand.get("cta", ""), links=_links_instruction(links), facts=_facts_instruction(facts, business)))
    if not md.lstrip().startswith("## "):
        md = f"## Next steps\n\n{md}"
    return md.strip()


def write_faq(llm: LLM, system: str, outline: Outline, facts: str = "", business: str = "the business") -> list[dict[str, str]]:
    if not outline.faq_questions:
        return []
    data = complete_json(llm, system, FAQ_PROMPT.format(
        h1=outline.h1, questions="\n".join(f"- {q}" for q in outline.faq_questions),
        facts=_facts_instruction(facts, business)))
    items = data.get("faq", data) if isinstance(data, dict) else data
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get("q") and item.get("a"):
            out.append({"q": str(item["q"]).strip(), "a": str(item["a"]).strip()})
    return out


def _plan(outline: Outline) -> str:
    return json.dumps([s.h2 for s in outline.sections])


def _tail(md: str, n: int = 80) -> str:
    words = md.split()
    return " ".join(words[-n:])


def business_name(brand: dict[str, Any], cfg: Config) -> str:
    return brand.get("business_name") or cfg.site.name or cfg.site.url


def write_draft(llm: LLM, brand: dict[str, Any], cfg: Config, outline: Outline, research: Research,
                intro_links: list[dict[str, str]], closing_links: list[dict[str, str]], facts: str = "") -> Draft:
    """`facts` is the general block from `facts.plan_facts`, which also fills each `section.facts`."""
    system = system_prompt(brand, cfg)
    business = business_name(brand, cfg)
    total = max(cfg.writing.min_words, min(cfg.writing.max_words, int((research.avg_words or cfg.writing.min_words) * 1.1)))
    budget = section_word_budget(outline, total)
    draft = Draft(outline=outline, facts=facts)
    draft.intro = write_intro(llm, system, outline, intro_links, facts, business)
    previous = draft.intro
    for section in outline.sections:
        section.markdown = write_section(llm, system, outline, section, research, budget, _tail(previous), business)
        previous = section.markdown
    draft.conclusion = write_conclusion(llm, system, outline, brand, closing_links, facts)
    draft.faq = write_faq(llm, system, outline, facts, business) if cfg.writing.faq else []
    return draft


def expand_section(llm: LLM, brand: dict[str, Any], cfg: Config, outline: Outline, section: Section, extra_words: int) -> str:
    system = system_prompt(brand, cfg)
    md = complete_text(llm, system, (
        f'Expand this section of "{outline.h1}" by about {extra_words} words with more concrete detail, examples '
        f"or steps. Keep every existing link and heading. Return the full revised section in markdown."
        f"{_facts_instruction(section.facts, business_name(brand, cfg))}\n{section.markdown}"
    ), max_tokens=max(1024, (word_count(section.markdown) + extra_words) * 3))
    return md if md.lstrip().startswith("## ") else section.markdown
