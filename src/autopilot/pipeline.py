"""The daily run: crawl if needed, pick a topic, research, write, link, score, repair, image, then review or publish."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from . import drift, health, indexnow
from .audit import daily_audit
from .brand import BRAND_KEY, build_brand_dna, load_brand
from .config import Config
from .crawl import crawl_site
from .facts import Grounding, plan_facts
from .db import DB, now
from .image import make_image
from .links import ensure_links, find_links, pick_external_links, plan_internal_links, strip_unknown_links
from .llm import LLM, complete_text
from .planning import route_topic, unique_value, weekly_cap
from .publish import PublishResult, can_update, make_publisher
from .refresh import apply_refresh, plan_refresh
from .indexing import fetch_posthog_conversions, inspect_urls
from .render import assemble_markdown, preview_html, schema_jsonld, to_html
from .research import research, search
from .score import ScoreReport, invented_experience, score_article, strip_invented_experience
from .textutil import clamp_text, word_count
from .topics import gsc_query, gsc_token, pick_topic, refresh_topics
from .writer import (Draft, apply_replacements, business_name, expand_section, make_outline, system_prompt, write_draft,
                     write_intro)

Log = Callable[[str], None]


class RunLog:
    def __init__(self, echo: Log | None = print):
        self.lines: list[str] = []
        self.echo = echo

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)
        if self.echo:
            self.echo(msg)

    def text(self) -> str:
        return "\n".join(self.lines)


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def article_uid(site_url: str, slug: str) -> str:
    """Stable idempotency key: re-sending the same article never creates a second post."""
    return hashlib.sha256(f"{host_of(site_url)}:{slug}".encode()).hexdigest()[:32]


def expected_url(cfg: Config, slug: str) -> str:
    site = cfg.site.url.rstrip("/")
    target = cfg.publish.target
    if target in ("markdown", "mdx", "nextjs"):
        return cfg.publish.markdown.url_pattern.format(site=site, slug=slug)
    if target == "shopify" and cfg.publish.shopify.blog_url:
        return f"{cfg.publish.shopify.blog_url.rstrip('/')}/{slug}"
    return f"{site}/{slug}/"


# -- onboarding ----------------------------------------------------------

def crawl(cfg: Config, db: DB, client: httpx.Client, log: Log = print) -> int:
    pages = crawl_site(client, cfg.site.url, cfg.site.sitemap_url, cfg.site.max_pages,
                       seeds=cfg.site.seed_urls, money_pages=cfg.site.money_pages)
    log(f"Crawled {len(pages)} pages from {cfg.site.url}")
    refresh_pages(cfg, db, client, pages, log)
    return len(pages)


def refresh_pages(cfg: Config, db: DB, client: httpx.Client, pages: list[dict[str, Any]], log: Log = print,
                  statuses: dict[str, int] | None = None) -> drift.DriftReport:
    """Store a fresh crawl, first diffing money pages against the stored copy so changed facts get flagged."""
    old = {p["url"]: p for p in db.pages()}
    confirmed_gone: list[str] = []

    def gone(url: str) -> bool:
        code = statuses[url] if statuses and url in statuses else health.fetch(client, url).status
        if code in (404, 410):
            confirmed_gone.append(url)
            return True
        return False

    rep = drift.compare(db, old, pages, gone)
    for p in pages:
        db.upsert_page(p)
    for url in confirmed_gone:
        db.delete_page(url)
    log(rep.summary())
    for s in rep.skipped:
        log(f"  skipped {s['url']}: {s['reason']}")
    for ch in rep.changes[:20]:
        log(f"  {ch.kind} on {ch.page_url}: {', '.join(ch.stale)} -> {', '.join(ch.fresh) or 'no longer stated'}")
    if rep.flagged:
        log(f"  {len(rep.flagged)} article sentence(s) quote an old fact; review with `autopilot facts list`")
    for m in rep.missing:
        log(f"  missing: {m}")
    return rep


def daily_check(cfg: Config, db: DB, client: httpx.Client, log: Log = print,
                token_fn: Callable[[Config], str] = gsc_token) -> health.HealthReport:
    """The site health check, then the money-page fact check on the pages it just fetched."""
    rows = None
    if cfg.topics.gsc_enabled:
        def rows() -> list[dict[str, Any]]:
            return gsc_query(client, token_fn(cfg), cfg.topics.gsc_site, cfg.health.gsc_days, ["page"], 5000)
    rep = health.check_site(cfg, client, db.get(health.LAST_KEY), rows)
    health.save(db, rep)
    log(rep.summary())
    for s in rep.failed_steps:
        log(f"  incomplete: {s.name} covered {s.actual}/{s.expected}. {s.detail}")
    for i in rep.errors[:10]:
        log(f"  {i.check}: {i.url} {i.detail}")
    if len(rep.errors) > 10:
        log(f"  ...and {len(rep.errors) - 10} more; see `autopilot health --last`")
    for src in rep.sources:
        log(f"  missing {src.name}: would unlock {src.unlocks}")
    if rep.pages:
        refresh_pages(cfg, db, client, rep.pages, log, rep.statuses)
    else:
        log("Fact check skipped: the health check fetched no pages to compare")
    return rep


def health_due(db: DB, hours: float = 20) -> bool:
    last = db.get(health.LAST_KEY)
    if not last:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last["started_at"])
    except (KeyError, ValueError):
        return True
    return age > timedelta(hours=hours)


def fix_fact_issue(cfg: Config, db: DB, client: httpx.Client, issue_id: int, llm: LLM | None = None,
                   log: Log = print) -> str:
    """Rewrite the article sentence that quotes a stale fact; also the live file for markdown targets.

    Returns the issue's new status: fixed, or patched when the live post on the CMS still needs the same edit."""
    issue = db.fact_issue(issue_id)
    if not issue:
        raise RuntimeError(f"No fact issue #{issue_id}")
    article = db.article(issue["article_id"])
    if not article:
        raise RuntimeError(f"Fact issue #{issue_id} points at missing article #{issue['article_id']}")
    siblings = [i["id"] for i in db.fact_issues(None, article["id"])
                if i["quote"] == issue["quote"] and i["status"] in ("open", "patched")]

    def close(status: str, note: str) -> str:
        for i in siblings:
            db.set_fact_issue(i, status, note)
        log(f"Fact issue #{issue_id} {status}: {note}")
        return status

    raw = drift._raw_sentence(article["markdown"], issue["quote"])
    if not raw:
        if article["status"] == "published" and issue["status"] == "open":
            return close("patched", f"the stored article no longer has the sentence, but the live post at "
                                    f"{article['remote_url']} may; check it, then `autopilot facts resolve {issue_id}`")
        return close("fixed", "the sentence is no longer in the article")
    page = next((p for p in db.pages() if p["url"] == issue["page_url"]), None)
    new = drift.corrected(raw, issue, (page or {}).get("text") or "", llm)
    markdown = drift.apply_fix(article["markdown"], raw, new)
    db.update_article(article["id"], markdown=markdown, html=to_html(markdown))
    action = f"replaced with: {new}" if new else "removed the sentence (the site no longer states that fact)"
    if article["status"] != "published":
        return close("fixed", action)

    if cfg.publish.target in ("markdown", "mdx", "nextjs") and article.get("remote_id"):
        path = Path(article["remote_id"])
        text = path.read_text() if path.exists() else ""
        if raw in text:
            path.write_text(drift.apply_fix(text, raw, new))
            note = action + f"; updated {path}"
            if cfg.publish.markdown.git_commit:
                from .publish.markdown import MarkdownPublisher

                error = MarkdownPublisher(cfg)._git(path, {**article, "h1": f"fact fix: {article['h1']}"})
                if error:
                    return close("patched", f"{note}, but {error}")
            return close("fixed", note)
        return close("patched", f"{action} in the stored copy; {path} no longer contains the old sentence, so check "
                                f"it by hand, then `autopilot facts resolve {issue_id}`")
    return close("patched", f"{action} in the stored copy. The live post at {article['remote_url']} still has the old "
                            f"sentence: edit it there, then run `autopilot facts resolve {issue_id}`")


def onboard(cfg: Config, db: DB, client: httpx.Client, llm: LLM, log: Log = print, recrawl: bool = False) -> dict[str, Any]:
    if recrawl or not db.pages():
        crawl(cfg, db, client, log)
    dna = build_brand_dna(llm, cfg.site.url, db.pages())
    db.set(BRAND_KEY, dna)
    log(f"Brand DNA saved for {dna.get('business_name') or cfg.site.url} ({len(dna.get('themes', []))} themes)")
    return dna


# -- writing -------------------------------------------------------------

def external_candidates(cfg: Config, client: httpx.Client, keyword: str, serp: list[dict[str, str]],
                        location: str = "") -> list[dict[str, str]]:
    own = host_of(cfg.site.url)
    n = cfg.writing.external_links
    country = cfg.site.country
    links = pick_external_links(serp, own, n, country, location, keyword)
    if len(links) < n and cfg.search.provider != "none":
        try:
            extra = search(client, cfg.search, f"{keyword} site:wikipedia.org OR site:.gov OR site:.edu",
                           cfg.site.language, cfg.site.country)["results"]
            seen = {l["url"] for l in links}
            links += [l for l in pick_external_links(extra, own, n, country, location, keyword) if l["url"] not in seen]
        except httpx.HTTPError:
            pass
    return links[:n]


def unique_slug(db: DB, slug: str) -> str:
    taken = {a["slug"] for a in db.articles()} | {p["url"].rstrip("/").rsplit("/", 1)[-1] for p in db.pages()}
    if slug not in taken:
        return slug
    i = 2
    while f"{slug}-{i}" in taken:
        i += 1
    return f"{slug}-{i}"


def score_draft(cfg: Config, draft: Draft, markdown: str, has_image: bool, grounding: Grounding | None = None) -> ScoreReport:
    o = draft.outline
    internal, external = find_links(markdown, cfg.site.url)
    return score_article(
        keyword=o.keyword, h1=o.h1, meta_title=o.meta_title, meta_description=o.meta_description, slug=o.slug,
        markdown=markdown, internal_links=len(set(internal)), external_links=len(set(external)),
        faq_count=len(draft.faq), has_image=has_image, min_words=cfg.writing.min_words,
        want_internal=cfg.writing.internal_links, want_external=cfg.writing.external_links, want_faq=cfg.writing.faq,
        seo_title=cfg.uses_seo_title, image_expected=cfg.image.provider.lower() != "site",
        brand_claims=grounding.claims(markdown) if grounding else None,
        unverified=grounding.domains(markdown) if grounding else None,
    )


def strip_unknown(draft: Draft, allowed_hosts: set[str], log: Log) -> None:
    """Unlink any website research never returned, everywhere in the draft."""
    removed: list[str] = []

    def fix(md: str) -> str:
        md, gone = strip_unknown_links(md, allowed_hosts)
        removed.extend(gone)
        return md

    draft.intro, draft.conclusion = fix(draft.intro), fix(draft.conclusion)
    for s in draft.outline.sections:
        s.markdown = fix(s.markdown)
    draft.faq = [{**f, "a": fix(f["a"])} for f in draft.faq]
    if removed:
        log(f"Unlinked {len(removed)} URL(s) research never returned: {', '.join(removed)}")


def repair(cfg: Config, llm: LLM, brand: dict[str, Any], draft: Draft, report: ScoreReport,
           intro_links: list[dict[str, str]], closing_links: list[dict[str, str]], log: Log) -> None:
    """One targeted pass over the failing checks. Cheap fixes are deterministic; content fixes are one LLM call each."""
    o = draft.outline
    failing = set(report.failing())
    business = brand.get("business_name") or cfg.site.name
    if "no_invented_experience" in failing:
        draft.intro = ensure_links(strip_invented_experience(draft.intro), intro_links)
        for s in o.sections:
            s.markdown = ensure_links(strip_invented_experience(s.markdown), s.links)
        draft.conclusion = ensure_links(strip_invented_experience(draft.conclusion), closing_links)
        draft.faq = [{**f, "a": strip_invented_experience(f["a"])} for f in draft.faq]
        log("repair: removed first-person anecdotes")
    if "h1_length" in failing:
        h1 = complete_text(llm, system_prompt(brand, cfg), (
            f'Rewrite this article title in 60 characters or fewer: "{o.h1}". Keep the exact phrase "{o.keyword}". '
            "Return only the title."
        ), max_tokens=60).strip().strip('"')
        if 30 <= len(h1) <= 60 and o.keyword.lower() in h1.lower():
            o.h1 = o.meta_title = h1
            log("repair: shortened H1")
        else:
            log(f"repair: H1 rewrite rejected ({len(h1)} chars or keyword missing)")
    if failing & {"keyword_in_meta_title", "meta_title_length", "meta_title_differs_from_h1"}:
        candidate = f"{o.keyword[:1].upper()}{o.keyword[1:]}: {business}" if business else f"{o.keyword.title()} Guide"
        if len(candidate) < 30:
            candidate = f"{o.keyword[:1].upper()}{o.keyword[1:]}: A Practical Guide"
        o.meta_title = clamp_text(candidate, 60)
        log("repair: rewrote meta title")
    if failing & {"keyword_in_meta_description", "meta_description_length"}:
        text = complete_text(llm, system_prompt(brand, cfg), (
            f'Write a meta description for "{o.h1}". 120-155 characters, includes the exact phrase "{o.keyword}", '
            "gives a reason to click. Return only the description."
        ), max_tokens=200).strip().strip('"')
        if o.keyword.lower() not in text.lower():
            text = f"{o.keyword[:1].upper()}{o.keyword[1:]}: {text}"
        o.meta_description = clamp_text(text, 158)
        log("repair: rewrote meta description")
    if "keyword_in_intro" in failing:
        draft.intro = ensure_links(write_intro(llm, system_prompt(brand, cfg), o, intro_links, draft.facts,
                                               business_name(brand, cfg)), intro_links)
        log("repair: regenerated intro")
    if "length" in failing:
        short = cfg.writing.min_words - word_count(assemble_markdown(draft))
        for section in sorted(o.sections, key=lambda s: word_count(s.markdown))[:3]:
            if short <= 0:
                break
            before = word_count(section.markdown)
            section.markdown = ensure_links(expand_section(llm, brand, cfg, o, section, max(150, short // 2)), section.links)
            short -= word_count(section.markdown) - before
        log("repair: expanded short sections")


def generate_article(cfg: Config, db: DB, client: httpx.Client, llm: LLM, keyword: str,
                     topic: dict[str, Any] | None = None, log: Log = print) -> int:
    brand = load_brand(db)
    log(f"Researching '{keyword}'")
    r = research(cfg, client, keyword)
    log(f"SERP: {len(r.competitors)} competitors, avg {r.avg_words} words, {len(r.questions)} questions")

    outline = make_outline(llm, brand, cfg, keyword, r)
    outline.slug = unique_slug(db, outline.slug)
    log(f"Outline: '{outline.h1}' / {len(outline.sections)} sections / slug {outline.slug}")

    intro_links, closing_links = plan_internal_links(
        outline, db.pages(), cfg.writing.internal_links, cfg.writing.embeddings,
        ranking_page=(topic or {}).get("ranking_page") or "",
    )
    external = external_candidates(cfg, client, keyword, r.serp, brand.get("location") or "")
    # citations go to sections that have no internal link yet, so no section is overloaded
    free = [s for s in outline.sections if not s.links] or outline.sections
    for i, link in enumerate(external):
        free[i % len(free)].links.append(link)
    log(f"Links planned: {len(intro_links) + len(closing_links) + sum(len(s.links) for s in outline.sections)}"
        f" ({len(external)} external)")

    pages = db.pages()
    facts = plan_facts(outline, pages, cfg.writing.embeddings)
    # websites the article may name: the site itself and whatever research returned
    allowed = {host_of(cfg.site.url)} | {host_of(u) for u in [x["url"] for x in r.serp] + [c.url for c in r.competitors]
                                         + [l["url"] for l in external]}
    grounding = Grounding(pages, brand, cfg.site.name, allowed)

    draft = write_draft(llm, brand, cfg, outline, r, intro_links, closing_links, facts)
    draft.external_links = external
    strip_unknown(draft, allowed, log)
    draft.intro = ensure_links(draft.intro, intro_links)
    for s in outline.sections:
        s.markdown = ensure_links(s.markdown, s.links)
    draft.conclusion = ensure_links(draft.conclusion, closing_links)

    image = None
    try:
        image = make_image(cfg.image, client, title=outline.h1, keyword=keyword, scene=outline.image_prompt,
                           style=cfg.site.image_style, out_dir=cfg.output_dir / "images", slug=outline.slug)
        if image:
            log(f"Image: {image.url or image.path}")
    except Exception as exc:  # an image failure should never cost us the article
        log(f"Image failed ({exc}); continuing without one")

    body = assemble_markdown(draft)
    report = score_draft(cfg, draft, body, bool(image), grounding)
    log(f"Score {report.score}/100" + (f", failing: {', '.join(report.failing())}" if report.failing() else ""))
    if report.failing():
        repair(cfg, llm, brand, draft, report, intro_links, closing_links, log)
        strip_unknown(draft, allowed, log)
        body = assemble_markdown(draft)
        report = score_draft(cfg, draft, body, bool(image), grounding)
        log(f"Score after repair {report.score}/100")
    if cfg.writing.replace:
        apply_replacements(draft, cfg.writing.replace)
        body = assemble_markdown(draft)
    invented = invented_experience(body)
    claims = grounding.claims(body)
    domains = grounding.domains(body)
    value = unique_value(body, pages, r)
    log(f"Unique value: {value.summary()}")
    no_value = cfg.writing.unique_value and not value.ok

    url = expected_url(cfg, outline.slug)
    schema = schema_jsonld(draft, url, brand, image.url if image else "")
    article = {
        "uid": article_uid(cfg.site.url, outline.slug),
        "topic_id": (topic or {}).get("id"),
        "keyword": keyword,
        "slug": outline.slug,
        "h1": outline.h1,
        "meta_title": outline.meta_title,
        "meta_description": outline.meta_description,
        "excerpt": outline.excerpt,
        "markdown": body,
        "html": to_html(body),
        "schema_json": json.dumps(schema),
        "score": report.score,
        "score_report": json.dumps(report.to_dict()),
        "image_path": image.path if image else "",
        "image_url": image.url if image else "",
        "image_alt": image.alt if image else "",
        "image_credit": image.credit if image else "",
        "status": "pending_review" if cfg.schedule.review or invented or claims or domains or no_value
                  or report.score < cfg.writing.min_score
                  else "approved",
        "remote_url": url,
        "unique_value": json.dumps(value.to_dict()),
    }
    if image and image.credit:
        article["html"] += f'\n<p class="image-credit"><small>{image.credit}</small></p>'
    article_id = db.insert_article(article)
    db.map_set(keyword, url, "article", replace=False)  # the query now has its one page
    if topic:
        db.set_topic_status(topic["id"], "used")
    path = write_preview(cfg, {**article, "id": article_id})
    log(f"Article #{article_id} saved ({article['status']}); preview: {path}")
    if article["status"] == "pending_review" and report.score < cfg.writing.min_score and not cfg.schedule.review:
        log(f"Held for review: score {report.score} is below min_score {cfg.writing.min_score}")
    if invented and not cfg.schedule.review:
        log(f"Held for review: still claims first-hand experience: {invented[0]}")
    if claims and not cfg.schedule.review:
        log(f"Held for review: {len(claims)} claim(s) about the business its own site doesn't support: {claims[0]}")
    if domains and not cfg.schedule.review:
        log(f"Held for review: names website(s) research never returned: {', '.join(domains)}")
    if no_value and not cfg.schedule.review:
        log("Held for review: no unique value. Add site data, a real price, a tool or a worked example the SERP lacks")
    return article_id


def write_preview(cfg: Config, article: dict[str, Any]) -> str:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.output_dir / f"{article['slug']}.html"
    preview = dict(article)
    if preview.get("image_path") and not preview.get("image_url"):
        preview["image_path"] = str(Path(preview["image_path"]).resolve())
    path.write_text(preview_html(preview, cfg.site.language))
    return str(path)


# -- publishing ----------------------------------------------------------

def indexnow_key(cfg: Config, db: DB) -> str:
    return cfg.indexnow.key or (db.get(indexnow.DB_KEY) or {}).get("key", "")


def ping_indexnow(cfg: Config, db: DB, client: httpx.Client, urls: list[str], log: Log = print) -> bool:
    key = indexnow_key(cfg, db)
    if not (cfg.indexnow.enabled and key and urls):
        return False
    location = (db.get(indexnow.DB_KEY) or {}).get("key_location", "")
    try:
        code = indexnow.submit(client, urls, key, location, cfg.indexnow.endpoint)
        log(f"IndexNow: submitted {len(urls)} URL(s), HTTP {code}")
        return True
    except (httpx.HTTPError, RuntimeError) as exc:
        log(f"IndexNow failed: {exc}")
        return False


def publish_article(cfg: Config, db: DB, client: httpx.Client, article_id: int, status: str | None = None,
                    log: Log = print) -> PublishResult | None:
    article = db.article(article_id)
    if not article:
        raise RuntimeError(f"No article #{article_id}")
    publisher = make_publisher(cfg, client)
    if publisher is None:
        db.update_article(article_id, status="approved")
        log(f"Article #{article_id} approved; publish.target is 'none', so nothing was sent")
        return None
    status = status or cfg.publish.status
    res = publisher.publish(article, status)
    live = res.status in ("publish", "published")
    db.update_article(
        article_id, status="published", remote_id=res.remote_id, remote_url=res.url or article["remote_url"],
        publish_status=res.status, published_at=now(), image_url=res.image_url or article["image_url"],
    )
    note = " (already existed, not duplicated)" if res.duplicate else ""
    log(f"Published #{article_id} to {cfg.publish.target} as {res.status}: {res.url}{note}")
    if res.git_error:
        log(f"Warning: {res.git_error}")
    if res.slug_conflict:
        log("Slug conflict: an existing post used this slug, so it was saved as a draft for you to check")
    if live and not res.duplicate and ping_indexnow(cfg, db, client, [res.url or article["remote_url"]], log):
        db.update_article(article_id, indexnow_at=now())
    return res


# -- the scheduled run ---------------------------------------------------

def next_topic(cfg: Config, db: DB, client: httpx.Client, brand: dict[str, Any], log: Log) -> dict[str, Any] | None:
    topic = pick_topic(db, cfg.topics.dedupe_threshold)
    if topic:
        return topic
    try:
        added = refresh_topics(cfg, db, client, brand, log=log)
        log(f"Topic queue refreshed: {added} new")
    except Exception as exc:
        log(f"Topic refresh failed: {exc}")
    return pick_topic(db, cfg.topics.dedupe_threshold)


def refresh_page(cfg: Config, db: DB, client: httpx.Client, llm: LLM, topic: dict[str, Any], url: str, mode: str,
                 log: Log = print) -> int | None:
    """Plan a refresh of the page that owns the topic. The diff is held for review; nothing changes on the site."""
    publisher = make_publisher(cfg, client)
    if not can_update(publisher):
        log(f"Can't refresh {url}: publish.target '{cfg.publish.target}' has no update path. No new page written")
        db.set_topic_status(topic["id"], "skipped")
        return None
    try:
        refresh_id = plan_refresh(cfg, db, client, llm, publisher, topic["keyword"], url, mode, topic, log)
    except (LookupError, RuntimeError, httpx.HTTPError) as exc:
        log(f"Refresh of {url} failed: {exc}. No new page written")
        db.set_topic_status(topic["id"], "skipped")
        return None
    db.map_set(topic["keyword"], url, "refresh", replace=False)
    db.set_topic_status(topic["id"], "used")
    return refresh_id


def apply_page_refresh(cfg: Config, db: DB, client: httpx.Client, refresh_id: int, log: Log = print) -> PublishResult:
    """Write a reviewed refresh to the site and tell IndexNow the page changed."""
    res = apply_refresh(cfg, db, make_publisher(cfg, client), refresh_id, log)
    ping_indexnow(cfg, db, client, [res.url or db.refresh(refresh_id)["target_url"]], log)
    return res


def _log_agents(llm: LLM, log: Log) -> None:
    """With provider "agent", record which signed-in CLI wrote this run and which ones were skipped."""
    summary = getattr(llm, "summary", None)
    if callable(summary):
        log(summary())


def run_once(cfg: Config, db: DB, client: httpx.Client, llm: LLM, keyword: str | None = None,
             publish: bool = True, echo: Log | None = print) -> list[int]:
    log = RunLog(echo)
    run_id = db.start_run()
    made: list[int] = []
    try:
        if cfg.health.daily and db.pages() and health_due(db):
            try:
                daily_check(cfg, db, client, log)
            except Exception as exc:  # the run goes on, but the failure is in the run log and the health history
                log(f"Health check failed: {exc!r}")
                health.record_failure(db, repr(exc))
        if not db.get(BRAND_KEY):
            onboard(cfg, db, client, llm, log)
        brand = load_brand(db)
        if cfg.topics.gsc_enabled and cfg.topics.gsc_site:
            try:  # keeps the index rate that paces new pages current
                inspect_urls(cfg, db, client, log, limit=200)
            except Exception as exc:
                log(f"URL Inspection skipped: {exc}")
        if cfg.measure.posthog_project_id:
            try:  # conversions per landing page, so the report judges pages by sign-ups
                fetch_posthog_conversions(cfg, db, client, 30)
            except Exception as exc:
                log(f"PostHog conversions skipped: {exc}")
        daily_audit(cfg, db, client, llm, log)  # one core page a day; logs its own failures
        attempts = 0
        done = 0
        wanted = 1 if keyword else max(1, cfg.schedule.posts_per_run)
        while done < wanted and attempts < wanted + 20:
            attempts += 1
            if keyword:
                db.add_topic(keyword, "manual", 100.0)
                topic = next(t for t in db.topics(None, 10_000) if t["keyword"] == keyword.strip().lower())
            else:
                topic = next_topic(cfg, db, client, brand, log)
            if not topic:
                log("No topics available. Add seeds in autopilot.toml or run `autopilot topics add`.")
                break
            route = route_topic(cfg, db, topic)
            if route.action == "skip":
                log(f"Skipped: {route.reason}")
                db.set_topic_status(topic["id"], "skipped")
                if keyword:
                    break
                continue
            if route.action == "refresh":
                log(f"Refresh first: {route.reason}")
                refresh_page(cfg, db, client, llm, topic, route.url, route.mode, log)
                done += 1
                if keyword:
                    break
                continue
            pace = weekly_cap(cfg, db)
            if pace.remaining <= 0:
                log(f"New-page cap reached ({pace.used}/{pace.cap} this week; {pace.reason}). "
                    f"'{topic['keyword']}' stays queued")
                break
            article_id = generate_article(cfg, db, client, llm, topic["keyword"], topic, log)
            done += 1
            made.append(article_id)
            if publish and db.article(article_id)["status"] == "approved":
                publish_article(cfg, db, client, article_id, log=log)
        _log_agents(llm, log)
        db.finish_run(run_id, "ok", log.text(), made[-1] if made else None)
    except Exception as exc:
        log(f"Run failed: {exc!r}")
        _log_agents(llm, log)
        db.finish_run(run_id, "failed", log.text(), made[-1] if made else None)
        raise
    return made
