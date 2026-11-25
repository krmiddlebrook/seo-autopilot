"""TOML configuration. Secrets are never stored in the file: each block names an env var."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class SiteConfig:
    url: str = ""
    name: str = ""
    language: str = "en"
    country: str = "us"
    image_style: str = "photo"  # clean|photo|watercolor|3d|vintage|abstract
    sitemap_url: str = ""
    max_pages: int = 300
    seed_urls: list[str] = field(default_factory=list)  # extra crawl starting points when there is no sitemap
    money_pages: list[str] = field(default_factory=list)  # path regexes for product/CTA pages, e.g. "^/demo"


@dataclass
class ScheduleConfig:
    posts_per_run: int = 1
    review: bool = True  # hold each article for `autopilot articles approve` instead of publishing


@dataclass
class LLMConfig:
    provider: str = "ollama"  # ollama|anthropic|openai|agent|fake
    model: str = "qwen3:14b"  # ignored by provider "agent": each CLI uses its own default unless agent_models names one
    base_url: str = ""
    api_key_env: str = ""
    temperature: float = 0.7
    timeout: float = 300.0
    max_tokens: int = 4096
    # provider "agent": signed-in agent CLIs, tried in this order until one answers
    agents: list[str] = field(default_factory=lambda: ["claude", "codex", "gemini"])
    agent_models: dict[str, str] = field(default_factory=dict)  # e.g. {codex = "gpt-5.5"}


@dataclass
class SearchConfig:
    provider: str = "searxng"  # searxng|dataforseo|serper|brave|none
    base_url: str = "http://localhost:8080"
    api_key_env: str = ""
    results: int = 5


@dataclass
class TopicsConfig:
    gsc_enabled: bool = False
    gsc_site: str = ""  # e.g. "sc-domain:example.com" or "https://example.com/"
    gsc_credentials: str = "gsc-credentials.json"
    gsc_days: int = 90
    min_position: float = 8.0
    max_position: float = 30.0
    min_impressions: int = 10
    autocomplete: bool = True
    seeds: list[str] = field(default_factory=list)
    semrush_api_key_env: str = ""
    dedupe_threshold: float = 0.6
    metrics_provider: str = ""  # empty preserves legacy semrush_api_key_env selection


@dataclass
class WritingConfig:
    min_words: int = 1200
    max_words: int = 2500
    internal_links: int = 4
    external_links: int = 2
    faq: bool = True
    faq_count: int = 5
    embeddings: str = "tfidf"  # tfidf|sentence-transformers
    min_score: int = 70
    style: str = ""  # extra house-style rules for every writing prompt
    replace: dict[str, str] = field(default_factory=dict)  # literal fixes on the finished text: {"—" = ", "}
    seo_title: bool = True  # false when the site uses the H1 as <title> and ignores a separate SEO title
    unique_value: bool = True  # hold a draft that names nothing the top results lack (site price/data, a tool, an example)


@dataclass
class ImageConfig:
    provider: str = "none"  # none|site|unsplash|pexels|openai|command (site = the CMS makes its own)
    api_key_env: str = ""
    model: str = "gpt-image-1"
    command: str = "mflux-generate --model schnell --steps 4 --width 1536 --height 864 --prompt {prompt} --output {output}"


@dataclass
class WordPressConfig:
    url: str = ""
    mode: str = "receiver"  # receiver (Soro-compatible plugin) | core (WP REST + Application Password)
    namespace: str = "autopilot/v1"  # or "soro/v1" when using Soro's own plugin
    api_key_env: str = "WP_RECEIVER_KEY"
    category: str = ""
    username: str = ""
    app_password_env: str = ""


@dataclass
class MarkdownConfig:
    dir: str = "content/blog"
    format: str = "mdx"  # md|mdx
    public_dir: str = ""  # Next.js public/ dir for images and IndexNow key
    image_url_prefix: str = "/images/blog"
    url_pattern: str = "{site}/blog/{slug}"
    frontmatter: str = "yaml"  # yaml | json (one JSON object between the --- lines)
    fields: dict[str, str] = field(default_factory=dict)  # rename frontmatter keys: {description = "meta_description"}
    defaults: dict[str, Any] = field(default_factory=dict)  # values for keys the article leaves empty (CTA, fallback image)
    image_ref: str = "path"  # path = image_url_prefix/name | filename = bare file name
    git_commit: bool = False
    git_push: bool = False


@dataclass
class GhostConfig:
    url: str = ""
    admin_key_env: str = "GHOST_ADMIN_KEY"
    tags: list[str] = field(default_factory=list)


@dataclass
class ShopifyConfig:
    shop: str = ""  # mystore.myshopify.com
    token_env: str = "SHOPIFY_ADMIN_TOKEN"
    blog_id: str = ""  # gid://shopify/Blog/123
    blog_url: str = ""  # https://mystore.com/blogs/news
    api_version: str = "2025-01"
    author: str = ""


@dataclass
class WebhookConfig:
    url: str = ""
    secret_env: str = ""


@dataclass
class PublishConfig:
    target: str = "none"  # wordpress|markdown|ghost|shopify|webhook|none
    status: str = "draft"  # draft|publish|pending|private
    wordpress: WordPressConfig = field(default_factory=WordPressConfig)
    markdown: MarkdownConfig = field(default_factory=MarkdownConfig)
    ghost: GhostConfig = field(default_factory=GhostConfig)
    shopify: ShopifyConfig = field(default_factory=ShopifyConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)


@dataclass
class HealthConfig:
    daily: bool = True  # run the site health check (and the money-page fact check) at the start of `autopilot run`
    min_words: int = 60  # less main text than this in the server HTML is flagged as content crawlers can't see
    max_urls: int = 500  # sitemap URLs checked per run; the report says when it stopped short
    gsc_days: int = 28


@dataclass
class IndexNowConfig:
    enabled: bool = True
    key: str = ""  # filled from the WP plugin's /setup-indexnow, or generated for static sites
    endpoint: str = "https://api.indexnow.org/indexnow"


@dataclass
class RefreshConfig:
    enabled: bool = True  # a query the site already ranks for updates that page instead of starting a new one
    min_position: float = 4.0  # positions min..max get a content refresh (gap sections, answer-first intro, links)
    max_position: float = 20.0
    ctr_ratio: float = 0.5  # above min_position, CTR under this share of the usual CTR for the position = rewrite the snippet
    min_impressions: int = 20
    max_sections: int = 3  # new sections per refresh
    max_new_links: int = 3
    cooldown_days: int = 30  # leave a refreshed page alone this long before planning another refresh


@dataclass
class PacingConfig:
    weekly_cap: int = 5  # new pages per rolling 7 days once the domain is established
    ramp: list[int] = field(default_factory=lambda: [1, 1, 2, 2, 3, 3, 4])  # cap for week 1, 2, ... on a new domain
    new_domain_pages: int = 20  # fewer content pages than this = a new domain, so the ramp applies
    index_min_age_days: int = 14  # only pages at least this old count toward the index rate
    min_sample: int = 5  # inspected pages needed before the index rate changes the cap
    slow_below: float = 0.6  # index rate under this halves the cap
    stop_below: float = 0.3  # index rate under this stops new pages until indexing recovers


@dataclass
class MeasureConfig:
    inspect_daily_quota: int = 2000  # Search Console URL Inspection allows 2,000 calls a day per site
    recheck_days: int = 7  # re-inspect a URL after this many days (not-indexed URLs are always due)
    horizon_months: int = 12
    conversions_csv: str = ""  # optional CSV with url,date,conversions (exported from GA4, a CRM...)
    ga4_property: str = ""  # optional GA4 property id: conversions (key events) per landing page via the Data API
    # PostHog: form submissions credited to the visitor's landing page (HogQL via the query API). Off when the
    # project id is empty. The key is a personal API key with the query:read scope, read from the named env var.
    posthog_project_id: str = ""
    posthog_host: str = "https://us.posthog.com"  # the app host (us/eu), not the i.posthog.com ingestion host
    posthog_api_key_env: str = "POSTHOG_PERSONAL_API_KEY"
    posthog_events: list[str] = field(default_factory=lambda: ["contact_form_submitted"])
    posthog_site: str = ""  # value of the `site` event property, for a project shared by several sites
    posthog_landing_property: str = "landing_page"  # first-touch landing path registered on every event
    posthog_referrer_property: str = "landing_referrer"  # first-touch referrer URL, for the channel split
    posthog_utm_prefix: str = "landing_utm_"  # first-touch UTM properties (<prefix>medium, <prefix>source)
    posthog_social_landings: list[str] = field(default_factory=list)  # paths only social links point to, e.g. "/links"
    target_index_rate: float = 0.8  # where the 12-month report wants each number to be
    target_monthly_clicks: int = 500  # clicks to money pages per month by the end of the horizon
    target_monthly_conversions: int = 20


@dataclass
class AuditConfig:
    enabled: bool = True  # each `autopilot run` audits one home or money page a day and proposes edits (never publishes)
    cooldown_days: int = 14  # audit the same page again only after this long
    gsc_days: int = 28  # Search Console window for the page's queries
    max_queries: int = 15
    min_impressions: int = 10  # a query needs this many impressions before CTR and position checks use it


@dataclass
class Config:
    site: SiteConfig = field(default_factory=SiteConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    topics: TopicsConfig = field(default_factory=TopicsConfig)
    writing: WritingConfig = field(default_factory=WritingConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    indexnow: IndexNowConfig = field(default_factory=IndexNowConfig)
    refresh: RefreshConfig = field(default_factory=RefreshConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    measure: MeasureConfig = field(default_factory=MeasureConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    # Where the config file lives; relative paths (db, output, credentials) resolve from here.
    root: Path = field(default_factory=Path.cwd)

    @property
    def db_path(self) -> Path:
        return self.root / "autopilot.db"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def uses_seo_title(self) -> bool:
        """Whether the publish target keeps a separate SEO title. Soro's plugin drops it, so the H1 is the <title>."""
        if self.publish.target == "wordpress" and self.publish.wordpress.namespace.startswith("soro/"):
            return False
        return self.writing.seo_title


def _build(cls: type, data: dict[str, Any]) -> Any:
    kwargs = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key '{key}' in [{cls.__name__}]")
        f = known[key]
        default = f.default_factory() if callable(f.default_factory) else f.default  # type: ignore[misc]
        kwargs[key] = _build(type(default), value) if is_dataclass(default) else value
    return cls(**kwargs)


def load_config(path: str | Path = "autopilot.toml") -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"No config at {path}. Run `autopilot init --url https://yoursite.com` first.")
    data = tomllib.loads(path.read_text())
    cfg = _build(Config, data)
    cfg.root = path.parent
    load_dotenv(cfg.root / ".env")
    cfg.search.provider = os.getenv("AUTOPILOT_SEARCH_PROVIDER") or cfg.search.provider
    cfg.topics.metrics_provider = os.getenv("AUTOPILOT_METRICS_PROVIDER") or cfg.topics.metrics_provider
    if cfg.topics.metrics_provider not in ("", "none", "dataforseo", "semrush"):
        raise ValueError("Choose none, dataforseo or semrush for the keyword metrics provider")
    return cfg


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def secret(env_name: str, required: bool = True) -> str:
    if not env_name:
        if required:
            raise RuntimeError("A secret is required but no env var name is configured")
        return ""
    value = os.environ.get(env_name, "")
    if required and not value:
        raise RuntimeError(f"Environment variable {env_name} is not set (add it to .env)")
    return value


CONFIG_TEMPLATE = """\
# seo-autopilot config. Secrets live in .env, referenced here by env var name.

[site]
url = "{url}"
name = "{name}"
language = "en"
country = "us"
image_style = "photo"     # clean | photo | watercolor | 3d | vintage | abstract
# sitemap_url = "{url}/sitemap.xml"
# seed_urls = ["/blog"]      # where the crawl starts if there is no working sitemap (homepage + /blog, /news... are tried anyway)
# money_pages = ["^/demo", "^/signup"]   # path regexes for pages posts should drive readers to (/services, /pricing... are built in)

[schedule]
posts_per_run = 1
review = true             # hold drafts for `autopilot articles approve <id>`; false = publish straight away

[llm]
provider = "ollama"       # ollama | anthropic | openai (any OpenAI-compatible API) | agent | fake
model = "qwen3:14b"
# provider = "agent"                    # whichever agent CLI is signed in: Claude Code, Codex (ChatGPT), Gemini
# agents = ["claude", "codex", "gemini"]  # tried in order; one that fails is skipped for the rest of the run
# provider = "anthropic"
# model = "claude-haiku-5-5"
# api_key_env = "ANTHROPIC_API_KEY"

[search]
provider = "searxng"      # searxng | dataforseo | serper | brave | none
base_url = "http://localhost:8080"
# provider = "serper"
# api_key_env = "SERPER_API_KEY"

[topics]
gsc_enabled = false
gsc_site = "sc-domain:{host}"
gsc_credentials = "gsc-credentials.json"   # service-account JSON or OAuth client secret
min_position = 8
max_position = 30
autocomplete = true
seeds = []                # extra seed keywords; Brand DNA themes are used automatically
# metrics_provider = "dataforseo"  # optional paid keyword metrics: dataforseo | semrush | none
# semrush_api_key_env = "SEMRUSH_API_KEY"

[writing]
min_words = 1200
max_words = 2500
internal_links = 4
external_links = 2
faq = true
min_score = 70
# style = "British English. No tables."   # extra house rules for the writer
# replace = {{ "—" = ", " }}             # literal fixes applied to the finished article
# seo_title = false                        # the site uses the H1 as <title>: score and repair the H1 instead
unique_value = true       # hold drafts with nothing the top results lack: a price or data from your site, a tool, an example

[image]
provider = "none"         # none | site (your CMS makes it) | unsplash | pexels | openai | command (mflux / ComfyUI CLI)
# api_key_env = "UNSPLASH_ACCESS_KEY"

[publish]
target = "none"           # wordpress | markdown | ghost | shopify | webhook | none
status = "draft"          # draft | publish | pending | private

[publish.wordpress]
url = "{url}"
mode = "receiver"         # receiver = autopilot-receiver plugin (or Soro's plugin); core = WP REST
namespace = "autopilot/v1"  # "soro/v1" if you installed Soro's own plugin
api_key_env = "WP_RECEIVER_KEY"
# username = "admin"                 # optional: enables direct media upload of local images
# app_password_env = "WP_APP_PASSWORD"

[publish.markdown]
dir = "content/blog"
format = "mdx"
public_dir = ""           # e.g. ../site/public  (images + IndexNow key file)
url_pattern = "{{site}}/blog/{{slug}}"
# frontmatter = "json"                          # yaml (default) | json, for sites that parse JSON front matter
# fields = {{ description = "meta_description" }}  # rename keys to match your site's schema
# defaults = {{ cta_label = "Get started" }}      # filled in when the article leaves a key empty

[indexnow]
enabled = true

[refresh]
enabled = true            # a query you already rank 4-20 for updates that page (held for review) instead of a new post
min_position = 4
max_position = 20
max_sections = 3

[audit]
enabled = true            # one home or money page a day: its searches, fixed checks, a proposed title/description/H1 and edits
cooldown_days = 14        # the report lands in output/audits/; `autopilot audits done ID` after you apply it

[pacing]
weekly_cap = 5            # new pages per 7 days; a new domain ramps up through `ramp`, and a falling index rate slows it
ramp = [1, 1, 2, 2, 3, 3, 4]

[measure]
inspect_daily_quota = 2000   # Search Console URL Inspection limit per site per day
horizon_months = 12
# conversions_csv = "conversions.csv"   # url,date,conversions
# ga4_property = "123456789"            # needs the analytics.readonly scope on the same credentials
# posthog_project_id = "12345"          # conversions from PostHog events, credited to each visitor's landing page
# posthog_events = ["contact_form_submitted"]
# posthog_site = "my-site"              # `site` event property, when one PostHog project serves several sites
# posthog_api_key_env = "POSTHOG_PERSONAL_API_KEY"   # personal API key with query:read, in .env
# posthog_social_landings = ["/links"]  # a link-in-bio page: conversions landing there count as social

[health]
daily = true              # check status codes, canonicals, noindex, orphans and server HTML before each run
min_words = 60            # main text below this in the raw HTML is flagged (JavaScript-only content)
max_urls = 500            # sitemap URLs checked per run
"""
