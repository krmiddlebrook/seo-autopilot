# Help more customers find your website

**What should I write so customers find me?**

## Find your first SEO fix with one prompt

Open Claude Code or Codex on your computer and paste this into a new chat. All you need to bring is your website address.

```text
Set up SEO Autopilot for my website from https://github.com/krmiddlebrook/seo-autopilot
Ask for my website address if you don't have it. Follow “First site setup” in the README.
Install it and show me one SEO fix that could help more customers find me. Point to
the page, tell me what you'd change, and explain why. Use my existing sign-in if you
need a model. Keep paid SEO services off. Let me review any edit before it goes live.
Give me the local report and the command to run the check again.
```

SEO Autopilot is a free, open-source SEO content audit tool for people who run a business and write its website. Pick a page where a customer can book, buy, or ask for a quote. Autopilot uses your Google Search Console queries and the pages that rank for them to suggest a specific edit. With PostHog connected, you can see which pages brought in inquiries from organic search.

At Poway Wrestling, we used it to refresh our blog. Within a month we ranked first on Google for a local signup search and moved from 25th to 8th for a regional wrestling search. Six people asked to join through our website that month, up from our usual one or two. Four became customers. [Read how we did it](https://kaithedatadaddy.com/posts/fix-the-page-your-buyers-already-find) · [Get the free worksheet](https://kaithedatadaddy.com/resources/seo-page-fix#signup)

## What can I do without an SEO subscription?

| If you're looking for… | What you can do with SEO Autopilot |
| --- | --- |
| A free Surfer SEO alternative for updating existing content | Audit a service page against the searches it already appears for. Review a proposed title, a better opening, or a missing section alongside the pages that rank for the same query. |
| A free Soro alternative for writing SEO articles | Build a queue from Search Console and autocomplete. If a page already covers the topic, review a refresh. If the site needs a new article, review a researched draft and publish it to your CMS. |

Surfer offers content audits and an editor. Soro offers scheduled article writing and publishing. SEO Autopilot covers those specific jobs on your own machine. The MIT-licensed software has no subscription fee. Use a local model via Ollama and self-host SearXNG for search results. You supply the machine and setup time. Paid services such as DataForSEO are optional. [Surfer Content Audit](https://docs.surferseo.com/en/articles/9182497-content-audit) · [Soro plans](https://trysoro.com/pricing)

Try one page before installing anything. In Search Console, filter the Search results report to your booking page and open **Queries**. Pick a question the page leaves unanswered. Write down the answer you would give a customer over the phone. Once you've set up the project, use the same page for your [first audit](#core-page-audits).

```
crawl → Brand DNA → topic queue (GSC striking distance + autocomplete + optional keyword metrics)
      → keyword map: does a page already own this query?
          yes → refresh that page (diff held for review), or skip it
          no  → cannibalization check → weekly cap → write:
                SERP research (top 5 pages) → outline → draft + FAQ → links
                → score + repair → unique-value check → image → publish → IndexNow
      → URL Inspection → report: index rate, money-page clicks, conversions vs. a 12-month plan
```

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # core
uv sync --extra gsc          # + Google Search Console
uv sync --extra embeddings   # + sentence-transformers for internal link matching
```

Then run commands as `uv run autopilot ...` (or activate `.venv`).

### First site setup

1. Clone this repository or reuse a local copy. Install uv if needed, then run `uv sync --locked --no-dev` in the checkout. Create a separate local folder for the website so its settings and reports stay private.
2. From the site folder, run `uv run --no-sync --project <checkout> autopilot init --url <website-url>`. Use the checkout's absolute path and the site's full URL. Reuse an existing config for this site if there is one. Run the remaining commands from the same folder with the same `uv run --no-sync --project <checkout> autopilot` prefix.
3. For this first run, set `AUTOPILOT_SEARCH_PROVIDER=none` and `AUTOPILOT_METRICS_PROVIDER=none` in the command environment. Run `crawl`, then `health`. Save the output of `health --last` as `seo-health.txt` in the site folder. A partial check still has a report. Tell the owner which pages you could reach.
4. For a suggested edit, use the owner's signed-in CLI. In `autopilot.toml`, set `[llm]` to `provider = "agent"` and `agents = ["claude"]` or `["codex"]` to match. Run `audits run --url <crawled-page-url>` on a page where a customer can book, buy, or get in touch. Share one finding, the proposed change, and the report in `output/audits/`. If the CLI is missing or signed out, share the health report now and explain how to enable the page audit. Keep publishing off for this first run.

## Quick start

```bash
uv run autopilot init --url https://yoursite.com --name "Your Business"
```

```bash
cp -n .env.example .env      # init writes the template next to autopilot.toml
```

```bash
uv run autopilot doctor      # checks the site, model, search, publish target
uv run autopilot health      # status codes, canonicals, noindex, orphans, server HTML
```

```bash
uv run autopilot onboard     # crawl, build Brand DNA, fill the topic queue
```

```bash
uv run autopilot run         # write the next article; held for review by default
```

```bash
uv run autopilot serve       # review dashboard at http://127.0.0.1:8787
```

Try the whole pipeline with no model, search engine or keys: `uv run autopilot --fake run --keyword "your topic" --dry-run`. It still needs to reach your site to crawl it.

## Configuration

Site settings live in `autopilot.toml`. Put credentials in the adjacent `.env` or export them in your shell. Shell values take precedence. Use a separate site directory for each customer.

### Connect DataForSEO

Get the API login and generated API password from the API Access tab in your DataForSEO account. In your local `.env`, fill in `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`. Enable either line below. Credentials alone leave billing off. [DataForSEO authentication](https://docs.dataforseo.com/v3/auth/)

```dotenv
AUTOPILOT_SEARCH_PROVIDER=dataforseo
AUTOPILOT_METRICS_PROVIDER=dataforseo
DATAFORSEO_LOCATION_CODE=2840
```

- **See who ranks.** The search option calls Google Organic Live Advanced for the top ten results, questions and related searches. It feeds article research, refresh plans and page audits. Each search is billable, including the test search in `autopilot doctor` and any follow-up citation search. [Google Organic Live Advanced](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/)
- **Prioritize topics.** The metrics option sends up to 50 distinct candidate keywords per topic refresh in one Labs Keyword Overview task. Available monthly search volume and keyword difficulty feed the topic score. Run `uv run autopilot topics refresh` after onboarding. [Keyword Overview](https://docs.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live/)
- **Choose your market.** Both options use `[site] language`. The US default is location code 2840. For other markets set a DataForSEO location code supported by the API you enable. [Labs locations and languages](https://docs.dataforseo.com/v3/dataforseo_labs-locations_and_languages/)
- **Use another provider.** The two environment selectors override `[search] provider` and `[topics] metrics_provider` independently. Use `none` to disable either. Existing SearXNG, Serper, Brave and Semrush configurations work as before.

### Crawling

The crawl reads your sitemap (from `robots.txt`, `/sitemap.xml`, `/sitemap_index.xml` or `/wp-sitemap.xml`, or `site.sitemap_url`). If there is no sitemap, or it errors or serves HTML, the crawl follows links from the homepage, `site.seed_urls` and common hubs (`/blog`, `/news`, `/articles`, `/posts`, `/guides`, `/resources`, `/learn`). Pages that serve identical HTML, such as a JavaScript app shell returned for every path, are kept once.

```toml
[site]
seed_urls = ["/journal"]              # extra starting points for the link crawl
money_pages = ["^/demo", "^/signup"]  # path regexes for pages posts should send readers to
```

`uv run autopilot doctor` warns when the sitemap is missing, when the homepage has almost no text or links before JavaScript runs (search engines may see an empty page too), when most crawled pages are thin, and when no money page was found. After changing `money_pages`, run `uv run autopilot crawl`.

### Model

| provider | notes |
| --- | --- |
| `ollama` (default) | Free. `ollama pull qwen3:14b`, then leave `base_url` empty for `http://localhost:11434`. |
| `anthropic` | `model = "claude-haiku-5-5"`, `api_key_env = "ANTHROPIC_API_KEY"`. Roughly a few cents per article. |
| `openai` | Any OpenAI-compatible API (OpenAI, OpenRouter, LM Studio, vLLM). Set `base_url` for non-OpenAI hosts. |
| `agent` | Whichever agent CLI you're signed in to: Claude Code (`claude`), Codex with ChatGPT (`codex`), Gemini (`gemini`). No API key. See below. |
| `fake` | Deterministic canned output for testing. Also enabled by the `--fake` flag. |

#### Signed-in agents

```toml
[llm]
provider = "agent"
agents = ["claude", "codex", "gemini"]   # tried in this order
# agent_models = { codex = "gpt-5.5" }   # optional; each CLI uses its own default otherwise
timeout = 300
```

Each prompt goes to the first agent that answers, billed to that CLI's own sign-in (Claude subscription, ChatGPT plan, Google account), not an API key: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the Gemini keys are removed from the CLI's environment. Calls are text in, text out. The CLI runs in an empty temp directory with tools off (`claude --tools ""`) or a read-only sandbox (`codex exec --sandbox read-only`, `gemini --approval-mode default`), so it can't edit the site. An agent that fails (not installed, signed out, out of quota, timed out) is skipped for the rest of the run and the next one takes over. If none answers, the run fails and says why for each. The run log records which agent wrote that run and which were skipped. `uv run autopilot doctor` tries each agent and shows which are usable. It's slower than an API (seconds per call for Claude, longer for Codex) and uses your plan's limits.

### SERP research

`searxng` is free and self-hosted:

SearXNG only answers `format=json` when `json` is listed under `search.formats`, and the image's default settings don't list it. Write your own `searxng/settings.yml`:

```yaml
use_default_settings: true
server:
  secret_key: "any-long-random-string"
  limiter: false
search:
  formats: [html, json]
```

Then mount it (change the host port if 8080 is taken, and match `search.base_url`):

```bash
docker run -d --name searxng -p 8080:8080 -v "$PWD/searxng:/etc/searxng" searxng/searxng
```

Other options include `dataforseo`, or `serper` and `brave` with `api_key_env`. `none` skips competitor research; the article is written from Brand DNA and autocomplete questions alone.

Research fetches the top 5 results with trafilatura and extracts their headings, word counts, shared subtopics and terms, plus People Also Ask style questions.

### Topics

Search Console and autocomplete supply the candidates. Paid metrics can help rank them.

1. **Search Console striking distance.** Queries where you rank in positions 8 to 30 with at least `min_impressions`, excluding branded queries. Each keeps the page that already ranks so Autopilot can decide whether to refresh it.
2. **Google autocomplete** for your Brand DNA themes and `seeds`.
3. **Optional keyword metrics.** DataForSEO or Semrush fills in available volume and difficulty for the topic queue.

```bash
uv run autopilot topics list
```

```bash
uv run autopilot topics add "gutter cleaning cost" "how often to clean gutters"
```

```bash
uv run autopilot topics skip 12
```

```bash
uv run autopilot topics refresh
```

**Search Console setup.** Install the `gsc` extra, set `gsc_enabled = true` and `gsc_site` (`sc-domain:yoursite.com` for a domain property, or the URL-prefix form). For `gsc_credentials`, either:

- a **service account** JSON key (Google Cloud console > IAM > Service accounts > Keys), with the service account's email added as a user on the property in Search Console; or
- an **OAuth desktop client** secret JSON. The first run opens a browser to consent and caches the token in `.gsc-token.json`.

Enable the "Google Search Console API" in the same Cloud project.

### Writing and scoring

Each article gets an outline (H1, a meta title that differs from the H1, meta description, slug, sections, FAQ questions), then one model call per section so long posts stay coherent on small local models. The FAQ is emitted as both content and `FAQPage` JSON-LD, alongside `BlogPosting`.

The scorer checks keyword placement (H1, meta title, first paragraph, slug, an H2), meta lengths, word count against `min_words`, heading structure, internal and external link counts, FAQ and image. If any check fails it runs one repair pass (meta title, meta description, intro, short sections) and rescores. Articles under `min_score` are always held for review, even with review off.

The model has no first-hand experience, so the writer is told not to claim any, and the scorer flags first-person anecdotes, made-up clients and testimonials ("I've watched lads...", "in my experience", "our clients tell us"). Repair deletes those sentences. If any survive, the article is held for review whatever its score.

The model also doesn't know the business, so it only gets to state what the business's own site says. Every writing prompt includes the crawled text of the relevant pages (the money pages, which carry prices and programs, plus the best matches for that section), and the writer is told to quote prices, ages and schedules exactly and to send readers to the business for anything that isn't there. The scorer's `no_unsupported_brand_claims` check then reads every sentence about the business: ones that name it, say "we" or "our", follow on with "it" or "they", or state a business fact (registration, fees, scholarships, spots, trial sessions, schedules...) in a paragraph that names it. A sentence fails if it gives a number the site never states, or if most of its words never appear on the site. "Brookvale Wrestling offers scholarships through the Club Circle foundation" fails when the site only describes the Club Circle as a paid membership. Any failing sentence holds the article for review whatever its score, with the sentences listed in the score report.

Links and website names are checked the same way. A link to a host that neither the site nor research returned is unlinked (the anchor text stays), and the `no_unverified_domains` check holds the article if any unknown domain is still named in the text, such as `usawrestlingmembership.com` written out by the model.

Some sites use the H1 as the page `<title>` and ignore a separate SEO title (Soro's WordPress plugin, many custom blog engines). Set `seo_title = false` and the outline keeps the H1 to 60 characters, the scorer checks H1 length instead of the meta title, and repair shortens a long H1. It's off automatically for `namespace = "soro/v1"`.

House rules go in `[writing]`: `style` is added to every writing prompt, and `replace` applies literal fixes to the finished article (title, meta, body, FAQ) for anything the model can't be trusted to avoid.

```toml
[writing]
style = "British English. No markdown tables."
replace = { "—" = ", ", "--" = "-" }
seo_title = false   # the site uses the H1 as <title>
```

### Links

- **Internal:** crawled pages ranked by TF-IDF (or sentence-transformers) similarity to each section. The best-matching money page (URLs like `/services/`, `/products/`, `/pricing/`, `/book/`, plus any `site.money_pages` patterns) goes in the closing section as the call to action. The page that already ranks for the keyword is always included.
- **External:** picked from the SERP results, preferring authority domains (.gov, .edu, .ac.*, .int, Wikipedia), then .org. Your own domain and ordinary commercial sites, which are usually competitors, are skipped. So are other countries' sites (by `site.country`), and city, county, state and parks-and-rec pages outside the business's area (the Brand DNA `location`): a San Diego club gets national sources and San Diego pages, never Fairfax County's rec department. The writer can only link to URLs it is given.

Re-crawl after publishing a batch so new posts join the internal link pool: `uv run autopilot crawl`.

### Images

| provider | notes |
| --- | --- |
| `none` | No image (the score loses a few points). |
| `site` | Your site makes its own (a generated hero, a template). No image, and no penalty. |
| `unsplash` / `pexels` | Free stock photos with credit, via `api_key_env`. |
| `openai` | `gpt-image-1`, styled by `site.image_style`. |
| `command` | Any local generator. The default runs mflux (FLUX schnell on Apple Silicon). Use `{prompt}` and `{output}` placeholders. |

## Publish targets

Set `[publish] target` and `status` (`draft`, `publish`, `pending` or `private`). Check the connection with `uv run autopilot verify`.

### WordPress (recommended: the bundled receiver plugin)

1. Zip the plugin folder: `cd wordpress-plugin && zip -r autopilot-receiver.zip autopilot-receiver`
2. In WordPress: Plugins > Add New > Upload Plugin, then activate it.
3. Settings > SEO Autopilot shows the API key. Put it in `.env` as `WP_RECEIVER_KEY`.
4. `uv run autopilot verify`, then `uv run autopilot indexnow setup` (the plugin generates and serves the key file).

The plugin is a drop-in for Soro's receiver API and also answers on `/wp-json/soro/v1/`. It adds what Soro's plugin lacks: a separate SEO title and JSON-LD schema. It writes SEO meta for Yoast, Rank Math and All in One SEO, sets the category and featured image (with alt text), and is idempotent: republishing the same article returns the existing post instead of creating a duplicate. A slug that already belongs to another post is saved as a draft with a suffix so nothing gets overwritten.

Other modes:

- **Soro's own plugin:** `namespace = "soro/v1"` with its key. Meta title and schema are dropped since it ignores them, so the H1 is scored as the title.
- **Core REST:** `mode = "core"` with `username` and an Application Password (`app_password_env`). Works with no plugin; SEO meta is written only if your SEO plugin exposes it to REST.

### Next.js / Astro / Hugo (Markdown or MDX)

`target = "markdown"`. Files go to `[publish.markdown] dir` with frontmatter (`title`, `seoTitle`, `description`, `date`, `slug`, `image`, `draft`, `faq`, `schema`). Set `public_dir` to your `public/` folder for images and the IndexNow key file, and `url_pattern` to match your routes. `git_commit` and `git_push` commit the new post so your host redeploys. Before pushing, autopilot runs `git pull --rebase` so commits made elsewhere (Replit, a teammate) do not block it. If the push still fails, the article is marked published, the log says what to run, and rerunning `articles approve <id>` retries the push without an empty commit.

For a custom blog engine, reshape the frontmatter to match what it reads:

```toml
[publish.markdown]
frontmatter = "json"                                      # one JSON object between the --- lines (default "yaml")
fields = { description = "meta_description", image = "hero_image" }  # rename keys
defaults = { cta_url = "https://yoursite.com/signup" }   # fill keys the article leaves empty
image_ref = "filename"                                    # bare file name instead of image_url_prefix/name
```

### Ghost

`target = "ghost"`, `[publish.ghost] url = "https://yourblog.com"`, and an Admin API key (Settings > Integrations > Add custom integration) in `GHOST_ADMIN_KEY`.

### Shopify

`target = "shopify"`. Create a custom app with `write_content` scope, put its Admin API token in `SHOPIFY_ADMIN_TOKEN`, and set `shop`, `blog_id` (`gid://shopify/Blog/...`) and `blog_url`.

### Webhook

`target = "webhook"` POSTs the full article JSON to `url`. With `secret_env` set, requests carry `X-Autopilot-Signature: sha256=<hmac of the body>`.

## IndexNow

Pings Bing, Yandex, Seznam and Naver when a post goes live (not for drafts or duplicates). Google does not use IndexNow; it picks posts up from your sitemap.

- WordPress with the receiver plugin: `uv run autopilot indexnow setup` fetches the key from the plugin, which serves `/{key}.txt`.
- Static sites: `indexnow setup` generates a key and writes the key file into `public_dir`.

Resubmit by hand with `uv run autopilot indexnow submit [urls...]`.

## Refresh before writing

A query that already has a page never gets a second one. Each run checks the keyword map (one page per query, filled from Search Console, published articles and `autopilot map set`) and the page Google already ranks for the query:

- **Position 4 to 20** (`[refresh] min_position`/`max_position`): a content refresh. Autopilot fetches the live page, compares it with the top five results, and plans up to `max_sections` missing sections, an answer-first intro when the page opens without one, and up to `max_new_links` new internal links.
- **Position under 4 with a click-through rate under `ctr_ratio` of the expected curve**: a title and description rewrite only. The body is left alone.
- **Anything else** (ranking well and getting clicks, or past position 20) is skipped. No new page.

Refreshes are never written straight to the site. Each one is saved as `pending_review` with a unified diff in `output/refresh-<id>.diff`:

```bash
uv run autopilot refresh list
```

```bash
uv run autopilot refresh show 2
```

```bash
uv run autopilot refresh apply 2
```

`apply` re-fetches the page and refuses if it changed since the refresh was planned, then updates it in place and pings IndexNow. `refresh reject 2` discards one, and `refresh plan "query" URL [--mode meta]` plans one by hand. A page refreshed in the last `cooldown_days` is left alone.

Updating works on every publish target: WordPress (receiver plugin `/content` and `/update` routes, or core REST), Markdown/MDX (rewrites the file and commits), Ghost (guarded by `updated_at`) and Shopify (`articleUpdate`). Soro's own plugin has no update route, so install the bundled receiver to refresh WordPress pages.

## New pages need evidence

- **Keyword map.** `uv run autopilot map list` shows which page owns each query. A new topic whose words overlap a mapped query (`[topics] dedupe_threshold`) is blocked as cannibalization before anything is written. Point it at a page with `map set "query" URL` to refresh that page instead. Crawled pages count even before the map has anything in it (no Search Console yet): a topic whose words all appear in a page's title, H1 or slug is routed to that page like a mapped query, and the log names the page it matched. The brand name that every title shares ("Contact Us | Brand") doesn't count as a match, and utility pages never own a query. A query made only of brand words belongs to the home page and is skipped. `map conflicts` lists near-identical queries already split across two pages.
- **Unique value.** After drafting, autopilot names what the draft has that the top results don't: a real price from your site, numbers from your own pages, a tool (comparison table, checklist, form) or a worked example. A draft with none of these is held for review even with `review = false`, and the reason is stored with it. Prices and numbers only count when research fetched competitor pages to compare against: with no search provider, or a search that found nothing, they show as **not verified** and the draft is held unless it also has a tool or a worked example. Turn it off with `[writing] unique_value = false`.
- **Pacing.** New pages are capped per rolling week (`[pacing] weekly_cap`). Until the site has `new_domain_pages` content pages the cap follows `ramp` (1, 1, 2, 2, 3, 3, 4 a week by default). When at least `min_sample` pages older than `index_min_age_days` have been checked, an index rate under `slow_below` halves the cap and under `stop_below` stops new pages until indexing recovers. Refreshes are not capped. A capped topic stays queued.

## Review flow

With `review = true` (the default) each run saves articles as `pending_review` and writes an HTML preview to `output/`.

```bash
uv run autopilot articles list
```

```bash
uv run autopilot articles preview 3
```

```bash
uv run autopilot articles publish 3
```

```bash
uv run autopilot articles reject 4
```

Or use `uv run autopilot serve`, a local dashboard (bound to 127.0.0.1) with the score breakdown, a rendered preview, and Publish and Reject buttons. Set `review = false` for full autopilot.

## Site health

`uv run autopilot health` checks the live site the way a crawler sees it, before any JavaScript runs:

- status codes for every sitemap URL (4xx, 5xx, no response, redirects and where they land)
- canonicals: missing, more than one, pointing off-site, or pointing at a page that does not return 200
- stray `noindex` in the meta robots tag or the `X-Robots-Tag` header on pages the sitemap asks to have indexed
- orphan pages (in the sitemap, linked from no other page) and broken internal links
- content missing from the server HTML: thin main text, byte-identical app shells, no `<h1>` or `<title>`

Each step reports what it covered against what it expected (URLs fetched vs. listed in the sitemap, Search Console rows returned), and a step that falls short marks the whole report `incomplete` rather than `ok`. A missing sitemap, a sitemap cut off at `health.max_urls`, a Search Console query that fails or returns zero rows: each one fails its step. The report ends with every data source it could not use and what that source would unlock (Search Console, URL Inspection, the previous report for a "what broke since yesterday" diff). The command exits 1 unless the report is `ok`, so it can gate a CI job.

With `health.daily = true` (the default) `autopilot run` runs the check once a day before writing, so the schedule you already set up covers it. A crash is logged and recorded in the history as `failed`; it never shows up as a quiet gap. `uv run autopilot health --last` prints the last report and up to two weeks of history; `--json` prints machine-readable output.

```toml
[health]
daily = true
min_words = 60     # main-text words below this flag a page as missing content in the server HTML
max_urls = 500     # sitemap URLs checked per run; more than this fails the sitemap step
```

## Fact drift

Articles quote prices, schedules and programs from your money pages. When the site changes, the article should too. Every crawl (`autopilot crawl` and the daily health check) compares each money page with its stored copy. A sentence whose price, time, weekday, month or number changed or disappeared becomes a fact change, and every article sentence that repeats the old value in the same context gets an open fact issue. Approved drafts that quote a stale fact go back to `pending_review` so they never publish.

The comparison never counts "nothing compared" as "nothing changed": the summary says how many money pages it compared out of how many it had, and why it skipped any (a page that was not fetched, or whose text collapsed, which usually means a rendering failure and not a price change). A money page counts as removed only when it returns 404 or 410.

```bash
uv run autopilot facts list            # open issues: stale values, what the site says now, the article's sentence
uv run autopilot facts show 3
uv run autopilot facts fix 3           # or: facts fix --all
uv run autopilot facts dismiss 3 --note "seasonal price, still valid"
uv run autopilot facts resolve 3       # after editing a live CMS post by hand
```

`fix` swaps the value when the change was a clean one-for-one edit ($375 to $400), otherwise asks the model for a corrected sentence and accepts it only if every value it states is on the current page, otherwise drops the sentence. For Markdown/MDX targets it also rewrites the published file (and commits it when `git_commit` is on). For WordPress, Ghost, Shopify and webhooks it updates the stored copy and marks the issue `patched`: edit the live post, then run `facts resolve`. `articles show` and `doctor` list open fact issues.

## Core-page audits

New articles only help if the pages that make money win their own searches. Once a day `autopilot run` audits one core page (the home page or a money page from `site.money_pages`): never-audited pages first, home before money pages, then whichever was audited longest ago. A page is not audited again within `audit.cooldown_days`.

For that page the audit pulls the last `audit.gsc_days` of Search Console queries for that exact URL, picks the top non-brand query as the target, looks at who ranks for it, and checks:

- whether the title, meta description and H1 contain the target query, and their lengths
- thin content against the pages that rank above it
- queries with a low click-through rate for their position, and queries in striking distance (positions `refresh.min_position` to `refresh.max_position`)
- money pages with clicks but no conversions, when PostHog or a conversions CSV is set up

The model then proposes a new title, description and H1 plus up to six section edits, using only facts that are on the page. Anything it needs but can't find goes into a list of questions for the owner instead of being made up, and any number in the proposal that the page doesn't state gets a warning. Nothing is applied to the site: the audit writes a report to `output/audits/` for you (or your site builder, Replit, Webflow) to apply.

```bash
uv run autopilot audits list                 # every audit, plus the page due next
uv run autopilot audits run                  # audit the next due page now (or --url https://...)
uv run autopilot audits show 4
uv run autopilot audits done 4 --note "new title live"   # pings IndexNow, reminds you to request indexing
uv run autopilot audits dismiss 4
```

After `done`, request indexing for the URL in Search Console (URL Inspection, then Request indexing); there's no API for that step. The next audit of the same page compares clicks, impressions, CTR and position with the earlier one, so you can see whether the change worked. A failed audit is logged and never stops the daily run.

```toml
[audit]
enabled = true
cooldown_days = 14
gsc_days = 28          # Search Console window per audit
max_queries = 15       # queries shown to the model and in the report
min_impressions = 10   # queries below this are ignored
```

## Scheduling

`uv run autopilot schedule launchd|cron|github --hour 7` prints a ready-to-install schedule:

- **launchd (macOS):** save the plist to `~/Library/LaunchAgents/` and `launchctl load` it. Runs even if you were asleep at the scheduled time.
- **cron:** add the line with `crontab -e`.

Both carry the `PATH` of the shell you ran `schedule` from, so `uv` and the agent CLIs resolve (launchd and cron otherwise start jobs with a bare `PATH`). Run it from a shell where `claude`/`codex` work.
- **GitHub Actions.** Install the generated workflow in a private operations repo. Give a persistent, dedicated self-hosted runner the label `seo-autopilot`, install `autopilot` on its PATH, and set repository variable `AUTOPILOT_CONFIG` to the absolute path of its local site config. The database and logs stay on that machine across runs. For a single laptop, cron or launchd uses the same local files.

## Reporting

Autopilot measures index rate and conversions, not page count.

```bash
uv run autopilot inspect
```

checks every published or refreshed URL with Search Console's URL Inspection API (needs `[topics] gsc_enabled` and `gsc_site`). It spends at most `[measure] inspect_daily_quota` calls a day (Google allows 2,000 per site), checks unverified and unindexed pages first, and rechecks indexed pages every `recheck_days`. Each run also inspects up to 200 URLs so pacing has a current index rate.

```bash
uv run autopilot report --days 28
```

reports the share of pages indexed, clicks to money pages (`[site] money_pages`) and conversions against a 12-month horizon (`horizon_months`, with targets `target_index_rate`, `target_monthly_clicks` and `target_monthly_conversions` paced linearly), followed by a per-page table. A page that couldn't be checked (no Search Console, a token error, an API error, or quota used up) shows as **not verified**, never as indexed. `--json` prints the same data for a dashboard.

Conversions come from a CSV (`url,date,conversions`, exported from GA4, a CRM or a form tool) or straight from GA4 key events by landing page:

```bash
uv run autopilot conversions import conversions.csv
```

```bash
uv run autopilot conversions ga4 --days 30
```

GA4 needs `[measure] ga4_property` and a token with the `analytics.readonly` scope.

Or from PostHog, crediting each conversion event to the visitor's first-touch landing page:

```bash
uv run autopilot conversions posthog --days 90
```

PostHog needs `[measure] posthog_project_id` and a personal API key with the `query:read` scope in the env var named by `posthog_api_key_env` (default `POSTHOG_PERSONAL_API_KEY`). `posthog_events` lists the events that count (default `["contact_form_submitted"]`); `posthog_site` filters on a `site` event property when one project serves several sites; `posthog_landing_property` names the first-touch path property (default `landing_page`, falling back to the session entry path, then the event's page). Test events (`analytics_test`) and other hosts are left out. Each pull replaces its window, `autopilot run` refreshes the last 30 days, and `autopilot report` refreshes before it counts.

Each PostHog conversion is also tagged with the channel that brought the visitor, from the first-touch UTM tags (`posthog_utm_prefix`, default `landing_utm_`) and referrer (`posthog_referrer_property`, default `landing_referrer`): `search` (Google, Bing, DuckDuckGo and other engines, or `utm_medium=organic`), `ai` (ChatGPT, Perplexity, Claude and similar), `social` (Instagram, Facebook and other networks, `utm_medium=social`, or a landing path listed in `posthog_social_landings`, such as a link-in-bio page at `/links`), `other` (another referring site or a paid campaign) and `direct` (no referrer recorded, which includes visits from before the site recorded referrers). The conversion goal in `autopilot report` counts search only, since that is what SEO moves; the report also lists every channel. Rows from a CSV or GA4 have no channel and count toward the goal as they are.

## Development

```bash
uv run pytest
```

The tests run fully offline: an `httpx.MockTransport` stands in for the site, autocomplete, IndexNow and a WordPress receiver, and the `fake` LLM provider drives the end-to-end run.

## Keeping a site private

Run each site's configuration, database, OAuth files, conversion exports and generated drafts outside the source checkout. The ignore rules cover the standard names, including SQLite sidecars. Keep custom credential filenames there too. When contributing, install Gitleaks 8.30.1 or later and enable the pre-push check in your clone. It scans outgoing commits for credentials and private file paths, including secrets removed by a later commit. [Gitleaks releases](https://github.com/gitleaks/gitleaks/releases)

```bash
git config --local core.hooksPath .githooks
```

If your clone already uses custom Git hooks, call this repository’s pre-push script from your existing pre-push hook. Review staged filenames with `git diff --cached --name-only`. If a secret entered history, rotate it with its provider before sharing the repository. Git ignore rules only apply to untracked files.

## Preparing a source release

Run the tests and review the tracked tree at the intended commit. Export a snapshot with the exact source revision you reviewed. The archive omits Git metadata and internal research notes.

```bash
git archive --format=zip --prefix=seo-autopilot/ -o seo-autopilot.zip HEAD
unzip -l seo-autopilot.zip
```

Unpack into an empty directory and scan it with `gitleaks dir --redact=100`. For a public GitHub release from a private development repository, initialize a new repository from the reviewed snapshot. Older branches, commits, and issue discussions can contain operator data even after the current files are clean.

The bundled WordPress receiver lives in `wordpress-plugin/`. Both it and the Python project ship under the [MIT license](LICENSE).
