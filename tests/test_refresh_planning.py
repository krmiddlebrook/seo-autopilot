import json
from datetime import datetime, timedelta, timezone

import httpx

from autopilot import indexing, pipeline, planning
from autopilot.llm import make_llm
from autopilot.refresh import refresh_mode
from autopilot.research import Competitor, Research

NOW = datetime.now(timezone.utc)


def publish_one(cfg, db, client, monkeypatch):
    monkeypatch.setenv("WP_RECEIVER_KEY", "test-key")
    cfg.schedule.review = False
    cfg.publish.target = "wordpress"
    cfg.publish.status = "publish"
    llm = make_llm(cfg.llm, client)
    [aid] = pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning cost", echo=None)
    a = db.article(aid)
    assert a["status"] == "published"
    return llm, a


def striking(db, keyword, url, position=8.0, impressions=600, clicks=6):
    db.add_topic(keyword, "gsc", 50.0, position=position, impressions=impressions, clicks=clicks, ranking_page=url)


def test_striking_distance_query_refreshes_instead_of_writing(cfg, db, client, web, monkeypatch):
    llm, a = publish_one(cfg, db, client, monkeypatch)
    striking(db, "gutter cleaning prices", a["remote_url"])

    made = pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning prices", echo=None)

    assert made == [] and len(db.articles()) == 1  # no new post
    [r] = db.refreshes()
    assert r["status"] == "pending_review" and r["mode"] == "content" and r["target_url"] == a["remote_url"]
    assert r["diff"].startswith("---") and "+" in r["diff"]
    assert (cfg.output_dir / f"refresh-{r['id']}.diff").exists()
    assert web.updates == 0  # held for review: nothing changed on the site
    assert db.map_owner("gutter cleaning prices") == a["remote_url"]

    pipeline.apply_page_refresh(cfg, db, client, r["id"], log=lambda *_: None)
    assert web.updates == 1 and db.refresh(r["id"])["status"] == "applied"


def test_refresh_refuses_when_post_changed_since_planning(cfg, db, client, web, monkeypatch):
    llm, a = publish_one(cfg, db, client, monkeypatch)
    striking(db, "gutter cleaning prices", a["remote_url"])
    pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning prices", echo=None)
    [r] = db.refreshes()
    next(iter(web.posts.values()))["body"]["content"] += "<p>edited by a human</p>"
    try:
        pipeline.apply_page_refresh(cfg, db, client, r["id"], log=lambda *_: None)
        raise AssertionError("expected a refusal")
    except RuntimeError as exc:
        assert "changed" in str(exc)
    assert web.updates == 0


def test_good_position_low_ctr_rewrites_title_and_description(cfg, db, client, web, monkeypatch):
    llm, a = publish_one(cfg, db, client, monkeypatch)
    assert refresh_mode(cfg.refresh, 2.0, 3, 1000) == "meta"
    striking(db, "gutter cleaning prices", a["remote_url"], position=2.0, impressions=1000, clicks=3)
    pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning prices", echo=None)
    [r] = db.refreshes()
    assert r["mode"] == "meta"
    ops = json.loads(r["ops"])
    assert ops and all(op["op"] in ("rewrite_title", "rewrite_description") for op in ops)
    assert r["new_body"] == r["old_body"]


def test_healthy_page_and_cannibalization_are_skipped(cfg, db, client, monkeypatch):
    llm, a = publish_one(cfg, db, client, monkeypatch)
    striking(db, "gutter cleaning prices", a["remote_url"], position=1.5, impressions=1000, clicks=400)
    topic = next(t for t in db.topics(None, 100) if t["keyword"] == "gutter cleaning prices")
    assert planning.route_topic(cfg, db, topic).action == "skip"
    # a near-duplicate of the mapped query is blocked before any writing
    route = planning.route_topic(cfg, db, {"keyword": "cost gutter cleaning"})
    assert route.action == "skip" and "cannibalize" in route.reason
    assert planning.route_topic(cfg, db, {"keyword": "roof repair insurance claims"}).action == "write"


def test_unique_value_names_signals_or_holds():
    pages = [{"title": "Gutter cleaning", "text": "Gutter cleaning from $149 per visit. We cleaned 340 homes last year."}]
    serp = Research("gutter cleaning", competitors=[Competitor("https://other.example/", "Gutter cleaning", "from $99")])
    good = planning.unique_value("Our visits start at $149. We cleaned 340 homes.\n\n| a | b |\n|---|---|\n| 1 | 2 |", pages, serp)
    assert good.ok and good.signals["price"] == ["$149"] and "340" in good.signals["site_data"]
    assert "comparison table" in good.signals["tool"]
    bare = planning.unique_value("Gutters matter. Clean them often. Prices vary, around $99.", [{"text": "no prices here"}], None)
    assert not bare.ok and "nothing the SERP lacks" in bare.summary()


def test_site_prices_are_not_verified_without_competitors():
    pages = [{"title": "Camp", "text": "Camp is $200 per week, $375 for two weeks. 48 wrestlers last summer."}]
    draft = "Camp costs $200 a week or $375 for two. We coached 48 wrestlers."
    for r in (None, Research("camp")):  # no search provider, or a search that found nothing
        v = planning.unique_value(draft, pages, r)
        assert not v.ok
        assert v.unverified["price"] == ["$200", "$375"] and "48" in v.unverified["site_data"]
        assert "not verified" in v.summary() and v.to_dict()["ok"] is False
    # a tool still counts on its own
    v = planning.unique_value(draft + "\n\n- [ ] Pack headgear", pages, None)
    assert v.ok and v.signals["tool"] == ["checklist"] and "not verified" in v.summary()


def crawled(db, url, title, h1=""):
    db.upsert_page({"url": url, "title": title, "h1": h1, "description": "", "text": "", "kind": "page",
                    "lastmod": "", "fetched_at": ""})


def test_crawled_page_owns_a_query_without_search_console(cfg, db):
    base = cfg.site.url.rstrip("/")
    crawled(db, f"{base}/juniors", "Brookvale Junior Squad | Youth Wrestling & Grappling Club San Diego | Brookvale Wrestling",
            "Brookvale Junior Squad: Built for Beginners")
    # shares "youth" and, through the brand suffix, "wrestling brookvale": a weaker match than the club page
    crawled(db, f"{base}/blog/youth-wrestlers-parent-guide", "Youth Wrestlers: A Parent's Starter Guide | Brookvale Wrestling")
    crawled(db, f"{base}/contact", "Contact Us | Brookvale Wrestling", "Get in Touch with Brookvale Wrestling")
    crawled(db, f"{base}/gear", "Official Gear | Brookvale Wrestling", "Official Brookvale Wrestling Gear")
    assert db.keyword_map() == []

    route = planning.route_topic(cfg, db, {"keyword": "youth wrestling brookvale"})
    assert route.action == "refresh" and route.url == f"{base}/juniors"
    assert "crawled page" in route.reason and "/juniors" in route.reason

    cfg.refresh.enabled = False
    route = planning.route_topic(cfg, db, {"keyword": "youth wrestling brookvale"})
    assert route.action == "skip" and "/juniors" in route.reason and "no new page" in route.reason


def test_branded_generic_title_does_not_block_new_pages(cfg, db):
    base = cfg.site.url.rstrip("/")
    crawled(db, f"{base}/contact", "Contact Us | Brookvale Wrestling", "Get in Touch with Brookvale Wrestling")
    crawled(db, f"{base}/gear", "Official Gear | Brookvale Wrestling", "Official Brookvale Wrestling Gear")
    crawled(db, f"{base}/alumni", "Alumni Hall of Fame | Brookvale Wrestling", "Meet Our Graduates.")
    for kw in ("brookvale wrestling summer camp", "brookvale wrestling tournament schedule", "wrestling weight classes"):
        assert planning.route_topic(cfg, db, {"keyword": kw}).action == "write", kw
    # a pure brand query is the home page's, never a new page
    crawled(db, f"{base}/", "Brookvale Wrestling | Youth Grappling Program", "Where Kids Start Wrestling")
    route = planning.route_topic(cfg, db, {"keyword": "brookvale wrestling"})
    assert route.action == "skip" and "brand query" in route.reason
    # with the brand configured, a single crawled page's suffix doesn't count either
    db.conn.execute("DELETE FROM pages")
    cfg.site.name = "Brookvale Wrestling"
    crawled(db, f"{base}/contact", "Contact Us | Brookvale Wrestling", "Get in Touch with Brookvale Wrestling")
    assert planning.route_topic(cfg, db, {"keyword": "brookvale wrestling camp"}).action == "write"


def test_draft_without_unique_value_is_held(cfg, db, client, monkeypatch):
    monkeypatch.setattr(pipeline, "unique_value", lambda *a, **k: planning.UniqueValue({}))
    cfg.schedule.review = False
    cfg.writing.unique_value = True
    llm = make_llm(cfg.llm, client)
    [aid] = pipeline.run_once(cfg, db, client, llm, keyword="roof repair insurance claims", echo=None)
    a = db.article(aid)
    assert a["status"] == "pending_review"
    assert json.loads(a["unique_value"])["ok"] is False


def add_articles(db, n, start, status="published"):
    for i in range(n):
        aid = db.insert_article({"uid": f"u{start}-{i}", "keyword": f"kw {start} {i}", "slug": f"s-{start}-{i}",
                                 "h1": "x", "status": status, "remote_url": f"https://example.com/s-{start}-{i}/"})
        db.conn.execute("UPDATE articles SET created_at=? WHERE id=?",
                        ((NOW - timedelta(days=start)).isoformat(timespec="seconds"), aid))
    db.conn.commit()


def test_weekly_cap_ramps_on_new_domain(cfg, db):
    assert planning.weekly_cap(cfg, db, NOW).cap == 1  # week 1
    add_articles(db, 1, 30)  # first page a month ago: week 5 of the ramp
    pace = planning.weekly_cap(cfg, db, NOW)
    assert pace.cap == cfg.pacing.ramp[4] and pace.used == 0
    add_articles(db, pace.cap, 1)
    assert planning.weekly_cap(cfg, db, NOW).remaining == 0


def test_cap_slows_then_stops_when_index_rate_drops(cfg, db):
    cfg.pacing.new_domain_pages = 0  # established domain: full weekly cap
    add_articles(db, 10, 60)
    urls = [a["remote_url"] for a in db.articles()]
    for i, u in enumerate(urls):
        db.save_inspection(u, "PASS" if i < 5 else "NEUTRAL", "Submitted and indexed" if i < 5 else "Crawled - currently not indexed")
    pace = planning.weekly_cap(cfg, db, NOW)
    assert pace.cap == max(1, cfg.pacing.weekly_cap // 2) and "halved" in pace.reason
    for u in urls[:4]:
        db.save_inspection(u, "NEUTRAL", "Discovered - currently not indexed")
    assert planning.weekly_cap(cfg, db, NOW).cap == 0


def test_cap_blocks_new_page_and_keeps_topic_queued(cfg, db, client):
    cfg.schedule.review = True
    llm = make_llm(cfg.llm, client)
    add_articles(db, 1, 0, status="pending_review")
    assert pipeline.run_once(cfg, db, client, llm, keyword="roof repair insurance claims", echo=None) == []
    assert next(t for t in db.topics(None, 100) if t["keyword"] == "roof repair insurance claims")["status"] == "queued"


def test_url_inspection_tracks_index_rate_and_quota(cfg, db, client):
    cfg.topics.gsc_site = "sc-domain:example.com"
    add_articles(db, 3, 30)
    seen = []

    def handler(request):
        url = json.loads(request.content)["inspectionUrl"]
        seen.append(url)
        if url.endswith("-2/"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        verdict = "PASS" if url.endswith("-0/") else "NEUTRAL"
        return httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {
            "verdict": verdict, "coverageState": "Submitted and indexed" if verdict == "PASS" else "Crawled - currently not indexed"}}})

    gsc = httpx.Client(transport=httpx.MockTransport(handler))
    cfg.measure.inspect_daily_quota = 2
    res = indexing.inspect_urls(cfg, db, gsc, lambda *_: None, token_fn=lambda *a, **k: "tok")
    assert len(seen) == 2 and res["checked"] + res["failed"] == 2  # quota caps calls
    cfg.measure.inspect_daily_quota = 2000
    indexing.inspect_urls(cfg, db, gsc, lambda *_: None, token_fn=lambda *a, **k: "tok")
    states = {u: indexing.state_of(r) for u, r in db.inspections().items()}
    assert sorted(states.values()) == ["indexed", "not indexed", "not verified"]
    assert indexing.index_rate(db, 14, NOW)[:2] == (1, 2)

    rep = indexing.build_report(cfg, db, None, today=NOW.date())
    text = indexing.format_report(rep)
    assert "not verified" in text and "12" in text


def test_pages_show_not_verified_without_search_console(cfg, db, client):
    cfg.topics.gsc_site = ""
    add_articles(db, 2, 30)
    indexing.inspect_urls(cfg, db, client, lambda *_: None)
    rep = indexing.build_report(cfg, db, None, today=NOW.date())
    assert {p["index"] for p in rep["pages"]} == {indexing.NOT_VERIFIED}


def test_conversions_csv_import(cfg, db, tmp_path):
    f = tmp_path / "conv.csv"
    f.write_text("url,date,conversions\n/services/gutter-cleaning/,2026-11-01,3\nhttps://example.com/blog/x/,2026-11-02,1\n")
    assert indexing.import_conversions_csv(db, f, "https://example.com") == 2
    assert sum(r["count"] for r in db.conversions()) == 4


def test_cli_map_refresh_inspect_report(tmp_path, capsys, monkeypatch):
    from autopilot.cli import main
    from autopilot.config import load_config
    from autopilot.db import DB

    monkeypatch.chdir(tmp_path)
    main(["init", "--url", "https://example.com", "--name", "Example Co"])
    main(["map", "set", "gutter cleaning cost", "https://example.com/a/"])
    main(["map", "set", "cost of gutter cleaning", "https://example.com/b/"])
    main(["map", "conflicts"])
    out = capsys.readouterr().out
    assert "https://example.com/b/" in out and "cost of gutter cleaning" in out
    d = DB(load_config(tmp_path / "autopilot.toml").db_path)
    add_articles(d, 1, 30)
    d.close()
    (tmp_path / "c.csv").write_text("url,date,conversions\nhttps://example.com/s-30-0/,2026-11-01,2\n")
    main(["conversions", "import", "c.csv"])
    main(["inspect"])
    main(["refresh", "list"])
    main(["report"])
    out = capsys.readouterr().out
    assert "Imported 1" in out and "not verified" in out and "12" in out


def test_draft_with_only_unverified_prices_is_held(cfg, db, client, web):
    # search provider "none": the fake draft quotes the site's $149, which can't be checked against any SERP
    cfg.schedule.review = False
    cfg.writing.unique_value = True
    llm = make_llm(cfg.llm, client)
    [aid] = pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning cost", echo=None)
    a = db.article(aid)
    value = json.loads(a["unique_value"])
    assert a["status"] == "pending_review" and value["ok"] is False
    assert value["unverified"]["price"] == ["$149"] and "not verified" in value["summary"]
