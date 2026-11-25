"""Deterministic stand-in for an LLM (`provider = "fake"`): exercises the whole pipeline with no model or API key."""

from __future__ import annotations

import json
import re

FILLER = (
    "Start with the situation you are actually in, then pick the option that fits it.",
    "A short checklist beats a long explanation when you are making the call on a busy day.",
    "Write down what you expect to happen, so you can compare it with what does happen.",
    "Small, repeatable steps matter more than one big push you cannot sustain.",
    "Ask a specialist when the stakes are high or the rules are unclear where you live.",
    "Most mistakes here come from skipping the boring preparation, not from bad luck.",
    "Keep receipts, photos and dates; they settle most disagreements quickly.",
    "If two options look equal, choose the one that is easier to undo later.",
)


def _quoted(prompt: str) -> str:
    m = re.search(r'"([^"]+)"', prompt)
    return m.group(1) if m else "the topic"


def _keyword(prompt: str) -> str:
    m = re.search(r'target(?:ing| query:)?\s*(?:the search query )?"([^"]+)"', prompt)
    return m.group(1) if m else _quoted(prompt)


def _links(prompt: str) -> list[str]:
    return [f"[{a}]({u})" for a, u in re.findall(r"^- \[([^\]]+)\]\(([^)]+)\)", prompt, flags=re.M)]


def _paragraphs(words: int, keyword: str, links: list[str], seed: int = 0) -> str:
    paras, count, i = [], 0, seed
    while count < words:
        sentences = [FILLER[(i + k) % len(FILLER)] for k in range(4)]
        sentences[0] = f"When it comes to {keyword}, " + sentences[0][0].lower() + sentences[0][1:]
        if links:
            sentences[1] = f"See {links.pop(0)} for the details. " + sentences[1]
        para = " ".join(sentences)
        paras.append(para)
        count += len(para.split())
        i += 3
    return "\n\n".join(paras)


def _outline(keyword: str) -> dict:
    title = keyword[:1].upper() + keyword[1:]
    parts = ["What it is and why it matters", "How to choose", "Step-by-step process", "Costs and timing",
             "Common mistakes", "Tools and resources"]
    return {
        "h1": f"{title}: A Practical Guide",
        "meta_title": f"{title} Explained, Step by Step",
        "meta_description": f"Everything you need to know about {keyword}: how to choose, what it costs, the steps "
                            "to follow and mistakes to avoid.",
        "slug": keyword.replace(" ", "-"),
        "excerpt": f"A practical guide to {keyword}, with steps, costs and common mistakes.",
        "sections": [{"h2": f"{p} ({keyword})" if i == 0 else p, "h3": [], "notes": ""} for i, p in enumerate(parts)],
        "faq": [f"How long does {keyword} take?", f"Is {keyword} worth it?", f"What does {keyword} cost?",
                f"Can I do {keyword} myself?", f"What should I ask before {keyword}?"],
        "image_prompt": f"a calm, well-lit workspace related to {keyword}",
    }


def fake_responder(system: str, prompt: str, json_mode: bool = False) -> str:
    if "Brand DNA" in prompt:
        site = re.search(r"from (\S+)", prompt)
        return json.dumps({
            "business_name": "Example Co", "one_liner": "Helps people get practical things done.",
            "audience": "busy homeowners", "location": "", "offerings": [{"name": "Consulting", "url": ""}],
            "tone": "plain, warm, direct", "voice_rules": ["second person", "short paragraphs"],
            "vocabulary": ["practical"], "avoid": ["hype"], "cta": f"Book a call at {site.group(1) if site else ''}",
            "themes": ["home maintenance", "budget planning", "seasonal checklists"],
        })
    if "answer-first opening" in prompt:
        kw = _keyword(prompt)
        return (f"Short answer: {kw} comes down to what you need done and when. " + " ".join(FILLER[:3]))
    if "Rewrite the search snippet" in prompt:
        kw = re.search(r'for "([^"]+)"', prompt)
        kw = kw.group(1) if kw else "this"
        title = kw[:1].upper() + kw[1:]
        return json.dumps({"title": f"{title}: Costs, Steps and What to Check",
                           "description": f"{title}, answered plainly: what it costs, how long it takes and the "
                                          "questions to ask before you book."})
    if "Plan an article" in prompt:
        return json.dumps(_outline(_keyword(prompt)))
    if "Answer these questions" in prompt:
        qs = re.findall(r"^- (.+\?)$", prompt, flags=re.M)
        return json.dumps({"faq": [{"q": q, "a": "Usually yes. " + " ".join(FILLER[:3])} for q in qs]})
    if "meta description" in prompt:
        kw = re.search(r'exact phrase "([^"]+)"', prompt)
        return f"Learn {kw.group(1) if kw else 'this'} step by step: what to check, what it costs and the mistakes most people make first."
    keyword = _keyword(prompt)
    links = _links(prompt)
    if "Write the introduction" in prompt:
        return f"If you are looking into {keyword}, you probably want a straight answer. " + _paragraphs(100, keyword, links)
    if "Write ONLY this section" in prompt:
        h2 = re.search(r"^## (.+)$", prompt, flags=re.M).group(1)
        words = int(re.search(r"about (\d+) words", prompt).group(1))
        body = _paragraphs(words, keyword, links, seed=len(h2))
        price = re.search(r"\$\d+(?:\.\d\d)?", prompt[prompt.find("Facts from"):] if "Facts from" in prompt else "")
        if price:
            body += f"\n\nFor reference, the site lists a price of {price.group(0)}."
        return f"## {h2}\n\n" + body
    if "closing section" in prompt:
        return "## Your next step\n\n" + _paragraphs(90, keyword, links, seed=5)
    if "Expand this section" in prompt:
        section = prompt[prompt.index("\n## ") + 1:]
        return section.rstrip() + "\n\n" + _paragraphs(200, keyword, [], seed=2)
    return "OK"
