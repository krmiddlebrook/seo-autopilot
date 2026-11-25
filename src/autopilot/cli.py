"""`autopilot` command line."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from . import audit, drift, health, indexnow, pipeline
from .brand import BRAND_KEY
from .config import CONFIG_TEMPLATE, Config, load_config
from .db import DB, now
from .http import make_client
from .llm import make_llm
from .topics import gsc_token, gsc_query, refresh_topics
from . import indexing, planning
from .publish import make_publisher
from .refresh import plan_refresh, refresh_mode

ENV_EXAMPLE = """\
# Copy to .env and fill in only the keys your autopilot.toml uses.
# Keep the AUTOPILOT_* and DATAFORSEO_* names as shown. Other credential names
# can be changed where autopilot.toml has a matching *_env setting.

# Model (pick one; Ollama needs no key)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# Paid SEO data (optional). Uncomment either selector to enable billable calls.
# AUTOPILOT_SEARCH_PROVIDER=dataforseo
# AUTOPILOT_METRICS_PROVIDER=dataforseo
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=
# Numeric market code. US defaults to 2840; for other markets set this explicitly.
# DATAFORSEO_LOCATION_CODE=2840

# SERP research (SearXNG needs no key)
SERPER_API_KEY=
BRAVE_API_KEY=

# Topics (optional)
SEMRUSH_API_KEY=

# Images (pick one, or none)
UNSPLASH_ACCESS_KEY=
PEXELS_API_KEY=

# Publish target (only the one you use)
WP_RECEIVER_KEY=
WP_APP_PASSWORD=
GHOST_ADMIN_KEY=
SHOPIFY_ADMIN_TOKEN=
WEBHOOK_SECRET=

# Optional conversion reporting
POSTHOG_PERSONAL_API_KEY=
"""


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.cfg: Config = load_config(args.config)
        if getattr(args, "fake", False):
            # offline smoke test: fake model, no web search, no image API
            self.cfg.llm.provider = "fake"
            self.cfg.search.provider = "none"
            self.cfg.image.provider = "none"
            self.cfg.topics.metrics_provider = "none"
        self.db = DB(self.cfg.db_path)
        self.client = make_client(timeout=self.cfg.llm.timeout)
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = make_llm(self.cfg.llm, self.client)
        return self._llm


def table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: min(60, max(len(c), *(len(str(r.get(c) if r.get(c) is not None else "")) for r in rows))) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) if r.get(c) is not None else "")[: widths[c]].ljust(widths[c]) for c in cols))


# -- commands ------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    path = Path(args.config)
    if path.exists() and not args.force:
        sys.exit(f"{path} already exists (use --force to overwrite)")
    url = args.url.rstrip("/")
    host = urlparse(url).netloc.removeprefix("www.")
    path.write_text(CONFIG_TEMPLATE.format(url=url, name=args.name or host, host=host))
    env_example = path.parent / ".env.example"
    if not env_example.exists():
        env_example.write_text(ENV_EXAMPLE)
    print(f"Wrote {path} and {env_example}. Next: edit the config, then run `autopilot onboard`.")


def cmd_onboard(args: argparse.Namespace) -> None:
    c = Ctx(args)
    dna = pipeline.onboard(c.cfg, c.db, c.client, c.llm, recrawl=args.recrawl)
    print(json.dumps(dna, indent=2, ensure_ascii=False))
    added = refresh_topics(c.cfg, c.db, c.client, dna)
    print(f"Queued {added} topics. Review them with `autopilot topics list`.")


def cmd_crawl(args: argparse.Namespace) -> None:
    c = Ctx(args)
    pipeline.crawl(c.cfg, c.db, c.client)


def cmd_brand(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.edit:
        dna = json.loads(Path(args.edit).read_text())
        c.db.set(BRAND_KEY, dna)
        print("Brand DNA replaced.")
        return
    print(json.dumps(c.db.get(BRAND_KEY) or {}, indent=2, ensure_ascii=False))


def cmd_topics(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "refresh":
        print(f"Added {refresh_topics(c.cfg, c.db, c.client, c.db.get(BRAND_KEY) or {})} topics")
    elif args.action == "add":
        for kw in args.values:
            print(("added: " if c.db.add_topic(kw, "manual", 90.0) else "exists: ") + kw)
    elif args.action == "skip":
        for tid in args.values:
            c.db.set_topic_status(int(tid), "skipped")
        print("Skipped.")
    else:
        rows = c.db.topics(None if args.all else "queued", args.limit)
        table(rows, ["id", "keyword", "source", "score", "position", "impressions", "volume", "kd", "status"])


def cmd_run(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.dry_run:
        c.cfg.schedule.review = True
    made = pipeline.run_once(c.cfg, c.db, c.client, c.llm, keyword=args.keyword, publish=not args.dry_run)
    for aid in made:
        a = c.db.article(aid)
        print(f"#{aid} {a['status']} score={a['score']} {a['h1']}")


def cmd_articles(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "list":
        table(c.db.articles(args.status), ["id", "status", "score", "keyword", "slug", "publish_status", "remote_url"])
        return
    if not args.id:
        sys.exit("article id required")
    a = c.db.article(args.id)
    if not a:
        sys.exit(f"No article #{args.id}")
    if args.action == "show":
        print(f"# {a['h1']}\nSEO title: {a['meta_title']}\nDescription: {a['meta_description']}\n"
              f"Slug: {a['slug']}  Score: {a['score']}  Status: {a['status']}\n")
        report = json.loads(a["score_report"] or "{}")
        for chk in report.get("checks", []):
            print(f"  {'✓' if chk['passed'] else '✗'} {chk['name']} {chk['detail']}")
        for i in c.db.fact_issues("open", a["id"]):
            print(f"  ✗ stale fact #{i['id']}: \"{i['quote']}\" (site now: {i['new_fact'] or 'no longer stated'})")
        print("\n" + a["markdown"])
    elif args.action == "preview":
        print(pipeline.write_preview(c.cfg, a))
    elif args.action == "reject":
        c.db.update_article(a["id"], status="rejected")
        if a["topic_id"]:
            c.db.set_topic_status(a["topic_id"], "skipped")
        print(f"Rejected #{a['id']}")
    elif args.action in ("approve", "publish"):
        pipeline.publish_article(c.cfg, c.db, c.client, a["id"], status=args.publish_status)


def cmd_health(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.last:
        last = c.db.get(health.LAST_KEY)
        if not last:
            sys.exit("No health check has run yet; run `autopilot health`")
        print(json.dumps(last, indent=2) if args.json else last.get("text") or last.get("summary"))
        history = c.db.get(health.HISTORY_KEY) or []
        if history and not args.json:
            print("\nHistory:")
            for h in history[-14:]:
                print(f"  {h['at'][:16]}  {h['status']:10} " + (h.get("error") or
                      f"{h['errors']} error(s), {h['warnings']} warning(s), {h['incomplete']} incomplete"))
        sys.exit(0 if last.get("status") == "ok" else 1)
    log = (lambda m: print(m, file=sys.stderr)) if args.json else print
    rep = pipeline.daily_check(c.cfg, c.db, c.client, log)
    print(json.dumps(rep.to_dict(), indent=2) if args.json else "\n" + rep.text())
    sys.exit(0 if rep.ok else 1)


def cmd_audits(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "list":
        rows = c.db.audits(None if args.status == "all" else args.status)
        for r in rows:
            r["problems"] = len(json.loads(r["findings"] or "[]"))
            r["edits"] = len(json.loads(r["proposal"] or "{}").get("edits", []))
            r["created"] = (r["created_at"] or "")[:10]
        table(rows, ["id", "status", "created", "kind", "url", "primary_query", "problems", "edits"])
        due = audit.pick_page(c.cfg, c.db)
        print(f"\nNext due: {due['url']}" if due else
              f"\nNo core page is due (each waits {c.cfg.audit.cooldown_days} days between audits).")
        return
    if args.action == "run":
        aid = audit.run_audit(c.cfg, c.db, c.client, c.llm, url=args.url or None)
        if aid:
            print(Path(c.db.audit(aid)["report_path"]).read_text())
        return
    if not args.id:
        sys.exit("audit id required")
    a = c.db.audit(args.id)
    if not a:
        sys.exit(f"No audit #{args.id}")
    if args.action == "show":
        path = Path(a["report_path"] or "")
        print(path.read_text() if path.is_file() else json.dumps(a, indent=2))
    elif args.action == "done":
        audit.mark_done(c.cfg, c.db, c.client, args.id, args.note)
    elif args.action == "dismiss":
        audit.dismiss(c.db, args.id, args.note or "dismissed by hand")
        print(f"Dismissed audit #{args.id}")


def cmd_facts(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "list":
        rows = c.db.fact_issues(None if args.status == "all" else args.status)
        for r in rows:
            r["stale"], r["now"] = ", ".join(r["old_values"]), ", ".join(r["new_values"]) or "(gone)"
        table(rows, ["id", "status", "article_id", "kind", "stale", "now", "page_url", "quote"])
        last = c.db.get(drift.LAST_KEY)
        if last:
            print(f"\nLast check {last['at'][:16]}: {last['summary']}")
        else:
            print("\nNo fact check has run yet: it runs after each crawl and daily health check.")
        return
    if args.action == "fix" and args.all:
        ids = [i["id"] for i in c.db.fact_issues("open")]
    elif args.id:
        ids = [args.id]
    else:
        sys.exit("issue id required (or --all with fix)")
    for iid in ids:
        issue = c.db.fact_issue(iid)
        if not issue:
            sys.exit(f"No fact issue #{iid}")
        if args.action == "show":
            a = c.db.article(issue["article_id"]) or {}
            print(f"#{iid} {issue['status']} on article #{issue['article_id']} {a.get('h1', '')}\n"
                  f"  page:     {issue['page_url']} ({issue['kind']})\n"
                  f"  site was: {issue['old_fact']}\n"
                  f"  site now: {issue['new_fact'] or '(no longer stated)'}\n"
                  f"  article:  {issue['quote']}" + (f"\n  note:     {issue['note']}" if issue["note"] else ""))
        elif args.action == "fix":
            if issue["status"] not in ("open",):
                print(f"#{iid} is {issue['status']}; skipping")
                continue
            try:
                llm = c.llm  # only needed when a clean value swap is not possible
            except Exception as exc:
                print(f"(no LLM: {exc}; sentences that cannot be swapped cleanly will be removed)")
                llm = None
            pipeline.fix_fact_issue(c.cfg, c.db, c.client, iid, llm)
        elif args.action == "dismiss":
            c.db.set_fact_issue(iid, "dismissed", args.note or "dismissed by hand")
            print(f"Dismissed #{iid}")
        elif args.action == "resolve":
            c.db.set_fact_issue(iid, "fixed", args.note or "live post corrected by hand")
            print(f"Resolved #{iid}")


def cmd_verify(args: argparse.Namespace) -> None:
    c = Ctx(args)
    target = c.cfg.publish.target
    if target == "wordpress":
        from .publish.wordpress import WordPressPublisher

        print(json.dumps(WordPressPublisher(c.cfg, c.client).verify(), indent=2))
    elif target == "ghost":
        from .publish.ghost import GhostPublisher

        g = GhostPublisher(c.cfg, c.client)
        r = c.client.get(f"{g.base}/site/", headers=g._headers())
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    elif target in ("markdown", "mdx", "nextjs"):
        d = c.cfg.resolve(c.cfg.publish.markdown.dir)
        print(f"Markdown dir {d} {'exists' if d.exists() else 'will be created'}")
    else:
        print(f"Nothing to verify for target '{target}'")


def cmd_indexnow(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "setup":
        info: dict[str, Any]
        if c.cfg.publish.target == "wordpress" and c.cfg.publish.wordpress.mode == "receiver":
            from .publish.wordpress import WordPressPublisher

            data = WordPressPublisher(c.cfg, c.client).setup_indexnow()
            info = {"key": data["key"], "key_location": data.get("key_location") or data.get("key_url", "")}
        else:
            key = c.cfg.indexnow.key or indexnow.new_key()
            info = {"key": key, "key_location": ""}
            public = c.cfg.publish.markdown.public_dir
            if c.cfg.publish.target in ("markdown", "mdx", "nextjs") and public:
                print(f"Wrote {indexnow.write_key_file(c.cfg.resolve(public), key)} (deploy it with your site)")
            else:
                print(f"Host a file at {c.cfg.site.url.rstrip('/')}/{key}.txt containing just: {key}")
        c.db.set(indexnow.DB_KEY, {**info, "created_at": now()})
        print(f"IndexNow key: {info['key']}")
    else:
        urls = args.urls or [a["remote_url"] for a in c.db.articles("published") if a["remote_url"]][: args.limit]
        if not pipeline.ping_indexnow(c.cfg, c.db, c.client, urls):
            sys.exit("Nothing submitted: run `autopilot indexnow setup` first, or pass URLs")


def cmd_report(args: argparse.Namespace) -> None:
    c = Ctx(args)
    rep = indexing.build_report(c.cfg, c.db, c.client, args.days)
    if args.json:
        print(json.dumps(rep, indent=2))
        return
    print(indexing.format_report(rep) + "\n")
    table(rep["pages"], ["index", "clicks", "impressions", "live", "checked", "coverage", "url"])


def cmd_inspect(args: argparse.Namespace) -> None:
    c = Ctx(args)
    indexing.inspect_urls(c.cfg, c.db, c.client, limit=args.limit)
    seen = c.db.inspections()
    table([{"url": u, "index": indexing.state_of(seen.get(u)), "coverage": (seen.get(u) or {}).get("coverage_state")
            or (seen.get(u) or {}).get("error") or ""} for u in indexing.tracked_urls(c.db)], ["index", "coverage", "url"])


def cmd_conversions(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "import":
        path = args.file or c.cfg.measure.conversions_csv
        if not path:
            sys.exit("conversions import needs a CSV path (url,date,conversions) or measure.conversions_csv")
        n = indexing.import_conversions_csv(c.db, c.cfg.resolve(path), c.cfg.site.url)
        print(f"Imported {n} row(s) from {path}")
    elif args.action == "ga4":
        if not c.cfg.measure.ga4_property:
            sys.exit("Set [measure] ga4_property to pull key events from GA4")
        print(f"Stored {indexing.fetch_ga4_conversions(c.cfg, c.db, c.client, args.days)} row(s) from GA4")
    elif args.action == "posthog":
        if not c.cfg.measure.posthog_project_id:
            sys.exit("Set [measure] posthog_project_id to pull conversion events from PostHog")
        n = indexing.fetch_posthog_conversions(c.cfg, c.db, c.client, args.days)
        print(f"Stored {n} landing page/day row(s) from PostHog ({', '.join(c.cfg.measure.posthog_events)})")
    else:
        table(c.db.conversions(), ["date", "count", "source", "url"])


def cmd_map(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "list":
        table(c.db.keyword_map(), ["query", "source", "url"])
    elif args.action == "set":
        if len(args.values) != 2:
            sys.exit('usage: autopilot map set "query" URL')
        c.db.map_set(args.values[0], args.values[1], "manual")
        print(f"'{args.values[0].lower()}' -> {args.values[1]}")
    elif args.action == "delete":
        for q in args.values:
            c.db.map_delete(q)
        print(f"Removed {len(args.values)} mapping(s)")
    elif args.action == "conflicts":
        rows = planning.map_conflicts(c.db, c.cfg.topics.dedupe_threshold)
        table([{"query": a, "url": u, "rival": b, "rival_url": v, "overlap": s} for a, u, b, v, s in rows],
              ["overlap", "query", "url", "rival", "rival_url"])


def cmd_refresh(args: argparse.Namespace) -> None:
    c = Ctx(args)
    if args.action == "list":
        rows = [{**r, "summary": json.loads(r["report"] or "{}").get("summary", "")} for r in c.db.refreshes(args.status)]
        table(rows, ["id", "status", "mode", "keyword", "summary", "target_url"])
        return
    if args.action == "plan":
        if len(args.values) != 2:
            sys.exit('usage: autopilot refresh plan "query" URL [--mode content|meta]')
        keyword, url = args.values
        topic = next((t for t in c.db.topics(None, 10_000) if t["keyword"] == keyword.strip().lower()), None) or {}
        mode = args.mode or refresh_mode(c.cfg.refresh, topic.get("position"), topic.get("clicks"),
                                         topic.get("impressions")) or "content"
        rid = plan_refresh(c.cfg, c.db, c.client, c.llm, make_publisher(c.cfg, c.client), keyword, url, mode, topic)
        if rid:
            c.db.map_set(keyword, url, "refresh", replace=False)
        return
    if not args.values:
        sys.exit("refresh id required")
    row = c.db.refresh(int(args.values[0]))
    if not row:
        sys.exit(f"No refresh #{args.values[0]}")
    if args.action == "show":
        report = json.loads(row["report"] or "{}")
        print(f"Refresh #{row['id']} ({row['mode']}, {row['status']}) for '{row['keyword']}'\n{row['target_url']}\n"
              f"{report.get('summary', '')}")
        for w in report.get("warnings", []):
            print(f"  check: {w}")
        print("\n" + row["diff"])
    elif args.action == "apply":
        if row["status"] != "pending_review":
            sys.exit(f"Refresh #{row['id']} is {row['status']}")
        pipeline.apply_page_refresh(c.cfg, c.db, c.client, row["id"])
    elif args.action == "reject":
        c.db.update_refresh(row["id"], status="rejected")
        print(f"Rejected refresh #{row['id']}")


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.seo-autopilot.daily</string>
  <key>ProgramArguments</key><array>
    <string>{exe}</string><string>--config</string><string>{config}</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>{root}/output/autopilot.log</string>
  <key>StandardErrorPath</key><string>{root}/output/autopilot.log</string>
</dict></plist>
"""

GITHUB = """name: seo-autopilot
on:
  schedule: [{{cron: "0 {hour} * * *"}}]
  workflow_dispatch:
permissions: {{}}
concurrency: seo-autopilot-daily
jobs:
  run:
    if: github.event.repository.private
    runs-on: [self-hosted, seo-autopilot]
    steps:
      # Install autopilot on this private runner and keep the site directory on its persistent disk.
      - name: Run with local settings
        env:
          SITE_CONFIG: ${{{{ vars.AUTOPILOT_CONFIG }}}}
        shell: bash
        run: |
          test -f "$SITE_CONFIG"
          umask 077
          cd "$(dirname "$SITE_CONFIG")"
          mkdir -p output
          autopilot --config "$SITE_CONFIG" run >>output/autopilot.log 2>&1
"""


def schedule_path() -> str:
    """launchd and cron start jobs with a bare PATH; pass this shell's PATH so uv and the agent CLIs resolve."""
    from .agents import EXTRA_BIN_DIRS

    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for d in (*EXTRA_BIN_DIRS, "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        d = str(Path(d).expanduser())
        if d not in parts:
            parts.append(d)
    return os.pathsep.join(parts)


def cmd_schedule(args: argparse.Namespace) -> None:
    cfg_path = Path(args.config).resolve()
    root = cfg_path.parent
    exe = shutil.which("autopilot") or f"{sys.executable} -m autopilot.cli"
    if args.kind == "launchd":
        print(LAUNCHD.format(exe=exe, config=cfg_path, root=root, hour=args.hour, path=escape(schedule_path())))
        print("<!-- Save as ~/Library/LaunchAgents/com.seo-autopilot.daily.plist, then:\n"
              "     launchctl load ~/Library/LaunchAgents/com.seo-autopilot.daily.plist -->", file=sys.stderr)
    elif args.kind == "cron":
        print(f"0 {args.hour} * * * cd {root} && PATH={shlex.quote(schedule_path())} {exe} --config {cfg_path} run "
              f">> {root}/output/autopilot.log 2>&1")
    else:
        print(GITHUB.format(hour=args.hour))


def agent_checks(c: Ctx, check, warn) -> None:
    """Provider "agent": show which signed-in CLIs answer. One is enough; the rest are fallbacks."""
    try:
        rows = c.llm.probe()
    except Exception as exc:  # bad [llm] agents list
        err = exc

        def fail() -> str:
            raise err

        check("LLM (agent)", fail)
        return
    usable = [name for name, ok, _ in rows if ok]
    for name, ok, detail in rows:
        if ok:
            print(f"✓ agent {name}: {detail}")
        else:
            warn(f"agent {name}", detail)
    first = usable[0] if usable else ""

    def summary() -> str:
        if not usable:
            raise RuntimeError("no signed-in agent answered; sign in to one of: " + ", ".join(n for n, _, _ in rows))
        return f"{first} writes, fallbacks: {', '.join(usable[1:]) or 'none'}"

    check("LLM (agent)", summary)


def cmd_doctor(args: argparse.Namespace) -> None:
    c = Ctx(args)
    cfg = c.cfg
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            print(f"✓ {name}{': ' + detail if detail else ''}")
        except Exception as exc:
            ok = False
            print(f"✗ {name}: {exc}")

    def warn(name: str, detail: str) -> None:  # worth fixing, but the pipeline still works
        print(f"! {name}: {detail}")

    check("site reachable", lambda: str(c.client.get(cfg.site.url).status_code))
    site_checks(c, cfg, warn)
    if cfg.llm.provider.lower() in ("agent", "agents"):
        agent_checks(c, check, warn)
    else:
        check(f"LLM ({cfg.llm.provider} {cfg.llm.model})", lambda: c.llm.complete("Reply with OK.", "Say OK.", max_tokens=10)[:40])
    if cfg.search.provider != "none":
        from .research import search

        check(f"search ({cfg.search.provider})", lambda: f"{len(search(c.client, cfg.search, 'test', cfg.site.language, cfg.site.country)['results'])} results")
    check("crawled pages", lambda: str(len(c.db.pages())))
    last = c.db.get(health.LAST_KEY)
    if not last:
        warn("site health", "no check has run yet; run `autopilot health` (it also runs daily inside `autopilot run`)")
    elif last.get("status") != "ok":
        warn("site health", f"{last.get('summary')} at {last['started_at'][:16]}; see `autopilot health --last`")
    else:
        print(f"✓ site health: {last.get('summary')} at {last['started_at'][:16]}")
    open_facts = c.db.fact_issues("open")
    if open_facts:
        warn("stale facts", f"{len(open_facts)} article sentence(s) quote a price, schedule or program the site no "
                            "longer states; see `autopilot facts list`")
    check("Brand DNA", lambda: (c.db.get(BRAND_KEY) or {}).get("business_name") or (_ for _ in ()).throw(RuntimeError("run onboard")))
    check("queued topics", lambda: str(len(c.db.topics("queued", 1000))))
    if cfg.topics.gsc_enabled:
        check("Search Console", lambda: f"{len(gsc_query(c.client, gsc_token(cfg), cfg.topics.gsc_site, 7, ['query'], 10))} rows")
    m = cfg.measure
    if m.posthog_project_id:
        def posthog_check() -> str:
            n = indexing.fetch_posthog_conversions(cfg, c.db, c.client, 30)
            total = sum(r["count"] for r in c.db.conversions(
                (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()) if r["source"].startswith("posthog"))
            return f"{total:g} conversion(s) over {n} landing page/day row(s) in 30 days ({', '.join(m.posthog_events)})"

        check(f"PostHog conversions (project {m.posthog_project_id})", posthog_check)
    elif not m.ga4_property and not m.conversions_csv:
        warn("conversions", "no source configured; set [measure] posthog_project_id (or ga4_property) so the "
                            "report counts sign-ups, not just traffic")
    if cfg.publish.target != "none":
        check(f"publish target ({cfg.publish.target})", lambda: cmd_verify(args) or "")
    sys.exit(0 if ok else 1)


def site_checks(c: Ctx, cfg: Config, warn) -> None:
    """Crawlability diagnostics that affect both this tool and how search engines see the site."""
    from .crawl import find_sitemap, homepage_health

    sm, entries = find_sitemap(c.client, cfg.site.url, cfg.site.sitemap_url)
    if sm:
        print(f"✓ sitemap: {sm} ({len(entries)} URLs)")
    else:
        warn("sitemap", "none found or it lists no URLs (checked robots.txt, /sitemap.xml, /sitemap_index.xml, "
                        "/wp-sitemap.xml). Search engines find new posts more slowly; the crawl follows links instead.")
    words, links = homepage_health(c.client, cfg.site.url)
    if words < 50 or links < 3:
        warn("homepage HTML", f"{words} words and {links} internal links before JavaScript runs. Search engines and "
                              "this crawler may see an empty page: prerender or server-render it, and set "
                              "site.seed_urls to sections that are server-rendered (e.g. /blog).")
    pages = c.db.pages()
    if pages:
        thin = sum(1 for p in pages if len((p.get("text") or "").split()) < 100)
        if thin > len(pages) / 2:
            warn("crawled pages", f"{thin} of {len(pages)} have under 100 words of text; internal links will be weak")
        if not any(p.get("kind") == "money" for p in pages):
            warn("money pages", "none found, so posts get no product or sign-up link. Set site.money_pages "
                                "(path regexes like \"^/pricing\"), then run `autopilot crawl`.")


def cmd_serve(args: argparse.Namespace) -> None:
    from .serve import serve

    c = Ctx(args)
    serve(c, args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Local SEO blog autopilot")
    p.add_argument("--config", default="autopilot.toml")
    p.add_argument("--fake", action="store_true", help="use the built-in fake LLM (no model needed)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write autopilot.toml")
    s.add_argument("--url", required=True)
    s.add_argument("--name", default="")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("onboard", help="crawl the site, build Brand DNA, fill the topic queue")
    s.add_argument("--recrawl", action="store_true")
    s.set_defaults(fn=cmd_onboard)

    sub.add_parser("crawl", help="re-crawl the site (refreshes the internal link index)").set_defaults(fn=cmd_crawl)

    s = sub.add_parser("brand", help="show Brand DNA, or replace it from an edited JSON file")
    s.add_argument("--edit", metavar="FILE")
    s.set_defaults(fn=cmd_brand)

    s = sub.add_parser("topics", help="manage the topic queue")
    s.add_argument("action", nargs="?", default="list", choices=["list", "refresh", "add", "skip"])
    s.add_argument("values", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_topics)

    s = sub.add_parser("run", help="write the next article(s); publish unless review is on")
    s.add_argument("--keyword")
    s.add_argument("--dry-run", action="store_true", help="write and save, never publish")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("articles", help="list / show / approve / reject / publish articles")
    s.add_argument("action", nargs="?", default="list", choices=["list", "show", "preview", "approve", "reject", "publish"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--status", help="filter for list")
    s.add_argument("--publish-status", choices=["draft", "publish", "pending", "private"])
    s.set_defaults(fn=cmd_articles)

    sub.add_parser("verify", help="check the publish target connection").set_defaults(fn=cmd_verify)

    s = sub.add_parser("indexnow", help="set up the IndexNow key or submit URLs")
    s.add_argument("action", choices=["setup", "submit"])
    s.add_argument("urls", nargs="*")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_indexnow)

    s = sub.add_parser("report", help="index rate, clicks to money pages and conversions against the 12-month plan")
    s.add_argument("--days", type=int, default=28)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("inspect", help="check published URLs with Search Console URL Inspection (2,000/day)")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("conversions", help="list, import a url,date,conversions CSV, or pull GA4 key events")
    s.add_argument("action", nargs="?", default="list", choices=["list", "import", "ga4", "posthog"])
    s.add_argument("file", nargs="?")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(fn=cmd_conversions)

    s = sub.add_parser("map", help="the keyword map: each query has exactly one page")
    s.add_argument("action", nargs="?", default="list", choices=["list", "set", "delete", "conflicts"])
    s.add_argument("values", nargs="*")
    s.set_defaults(fn=cmd_map)

    s = sub.add_parser("refresh", help="list / show / apply / reject page refreshes, or plan one")
    s.add_argument("action", nargs="?", default="list", choices=["list", "plan", "show", "apply", "reject"])
    s.add_argument("values", nargs="*")
    s.add_argument("--status")
    s.add_argument("--mode", choices=["content", "meta"])
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("schedule", help="print a launchd / cron / GitHub Actions schedule")
    s.add_argument("kind", choices=["launchd", "cron", "github"])
    s.add_argument("--hour", type=int, default=7)
    s.set_defaults(fn=cmd_schedule)

    sub.add_parser("doctor", help="check every dependency the run needs").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("health", help="check status codes, canonicals, noindex, orphans and server HTML; then facts")
    s.add_argument("--last", action="store_true", help="show the last stored report instead of running a check")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("facts", help="articles that quote a price, schedule or program the site changed")
    s.add_argument("action", choices=["list", "show", "fix", "dismiss", "resolve"])
    s.add_argument("id", type=int, nargs="?")
    s.add_argument("--all", action="store_true", help="with fix: fix every open issue")
    s.add_argument("--status", default="open", choices=["open", "patched", "fixed", "dismissed", "all"])
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_facts)

    s = sub.add_parser("audits", help="core-page audits: edits for the home and money pages, one page a day")
    s.add_argument("action", choices=["list", "run", "show", "done", "dismiss"])
    s.add_argument("id", type=int, nargs="?")
    s.add_argument("--url", default="", help="with run: audit this page instead of the next due one")
    s.add_argument("--status", default="all", choices=["open", "applied", "dismissed", "all"])
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_audits)

    s = sub.add_parser("serve", help="local review dashboard")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
