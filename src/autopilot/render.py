"""Assemble the article markdown, render HTML, and build Article + FAQPage JSON-LD."""

from __future__ import annotations

import html
import json
from typing import Any

import markdown as md_lib

from .writer import Draft


def assemble_markdown(draft: Draft, include_h1: bool = False) -> str:
    """Body markdown. The H1 is the CMS title, so it is left out of the body by default."""
    o = draft.outline
    parts = [f"# {o.h1}"] if include_h1 else []
    parts.append(draft.intro)
    parts += [s.markdown for s in o.sections]
    if draft.faq:
        parts.append("## Frequently asked questions\n\n" + "\n\n".join(f"### {f['q']}\n\n{f['a']}" for f in draft.faq))
    parts.append(draft.conclusion)
    return "\n\n".join(p.strip() for p in parts if p and p.strip()) + "\n"


def to_html(markdown_text: str) -> str:
    return md_lib.markdown(markdown_text, extensions=["tables", "sane_lists", "smarty"], output_format="html")


def schema_jsonld(draft: Draft, url: str, brand: dict[str, Any], image_url: str = "", published: str = "") -> list[dict[str, Any]]:
    o = draft.outline
    article: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": o.h1,
        "description": o.meta_description,
        "mainEntityOfPage": url,
        "keywords": o.keyword,
        "publisher": {"@type": "Organization", "name": brand.get("business_name", "")},
    }
    if image_url:
        article["image"] = image_url
    if published:
        article["datePublished"] = published
    out = [article]
    if draft.faq:
        out.append({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
                for f in draft.faq
            ],
        })
    return out


def jsonld_script(schema: list[dict[str, Any]]) -> str:
    scripts = []
    for item in schema:
        payload = json.dumps(item, ensure_ascii=False).replace("</", "<\\/")
        scripts.append(f'<script type="application/ld+json">{payload}</script>')
    return "\n".join(scripts)


PREVIEW = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{meta_title}</title><meta name="description" content="{meta_description}">
{jsonld}
<style>
:root{{color-scheme:light dark;--fg:#1d1d1f;--bg:#fbfbf9;--muted:#6b6b6b;--accent:#2f5d50;--card:#f0efe9}}
@media (prefers-color-scheme:dark){{:root{{--fg:#ececea;--bg:#151615;--muted:#a3a3a0;--accent:#8fc3b1;--card:#20221f}}}}
body{{font:18px/1.65 Georgia,serif;color:var(--fg);background:var(--bg);max-width:720px;margin:0 auto;padding:24px 16px 80px}}
h1,h2,h3{{font-family:system-ui,sans-serif;line-height:1.25}} a{{color:var(--accent)}} img{{max-width:100%;border-radius:8px}}
.meta{{font:14px/1.5 system-ui,sans-serif;background:var(--card);padding:12px 16px;border-radius:8px;color:var(--muted)}}
.meta b{{color:var(--fg)}} table{{border-collapse:collapse}} td,th{{border:1px solid var(--muted);padding:4px 8px}}
</style></head><body>
<div class="meta"><b>SEO title:</b> {meta_title}<br><b>Description:</b> {meta_description}<br>
<b>Slug:</b> /{slug} &nbsp; <b>Keyword:</b> {keyword} &nbsp; <b>Score:</b> {score}/100</div>
{image}<h1>{h1}</h1>
{body}
</body></html>"""


def preview_html(article: dict[str, Any], lang: str = "en") -> str:
    img = ""
    src = article.get("image_url") or article.get("image_path")
    if src:
        img = f'<p><img src="{html.escape(src)}" alt="{html.escape(article.get("image_alt") or "")}"></p>'
    schema = json.loads(article.get("schema_json") or "[]")
    return PREVIEW.format(
        lang=lang,
        meta_title=html.escape(article["meta_title"]),
        meta_description=html.escape(article["meta_description"]),
        slug=html.escape(article["slug"]),
        keyword=html.escape(article["keyword"]),
        score=article.get("score", "?"),
        h1=html.escape(article["h1"]),
        image=img,
        jsonld=jsonld_script(schema),
        body=article["html"],
    )
