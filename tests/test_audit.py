"""Core-page audits: one home or money page a day, checked against the searches it should win."""

import json
from datetime import datetime, timedelta, timezone

from autopilot import audit, pipeline

NOW = datetime.now(timezone.utc)
SERVICE = "https://example.com/services/gutter-cleaning/"
HOME = "https://example.com/"


class StubLLM:
    def __init__(self, answer: dict):
        self.answer, self.prompts = answer, []

    def complete(self, system, prompt, json_mode=False, max_tokens=None):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


PROPOSAL = {
    "title": "Gutter Cleaning in Example Town | From $149 per Visit | Example Co Professional Services",
    "description": "Professional gutter cleaning from $149 per visit.",
    "h1": "Gutter cleaning service",
    "edits": [{"section": "Pricing", "change": "Say visits take 45 minutes", "why": "answers how long"}],
    "needs_owner": ["How long does a visit take?"],
}


def row(query, clicks, impressions, position):
    return {"keys": [query], "clicks": clicks, "impressions": impressions, "ctr": clicks / max(1, impressions),
            "position": position}


def rows_for(url):
    if url == SERVICE:
        return [row("gutter cleaning", 2, 400, 3.2), row("gutter cleaning near me", 1, 120, 9.0),
                row("example co", 30, 60, 1.0)]
    return []


def crawled(cfg, db, client):
    pipeline.crawl(cfg, db, client, log=lambda *_: None)


def test_core_pages_are_home_and_money_only(cfg, db, client):
    crawled(cfg, db, client)
    urls = {p["url"] for p in audit.core_pages(db)}
    assert urls == {HOME, SERVICE}


def test_pick_page_prefers_never_audited_then_oldest_and_respects_cooldown(cfg, db, client):
    crawled(cfg, db, client)
    db.insert_audit({"url": SERVICE, "status": "open", "created_at": (NOW - timedelta(days=3)).isoformat()})
    assert audit.pick_page(cfg, db, NOW)["url"] == HOME
    db.insert_audit({"url": HOME, "status": "open", "created_at": (NOW - timedelta(days=1)).isoformat()})
    assert audit.pick_page(cfg, db, NOW) is None  # both inside the 14-day cooldown
    assert audit.pick_page(cfg, db, NOW + timedelta(days=12))["url"] == SERVICE  # 15 days old, home is 13


def test_findings_cover_snippet_terms_ctr_striking_distance_and_conversions(cfg):
    queries = audit.to_queries(rows_for(SERVICE))
    page = {"url": SERVICE, "title": "Our Services", "description": "", "h1": "Services", "h1_count": 1,
            "words": 120}
    found = {f["check"]: f for f in audit.findings(cfg, page, queries, primary=queries[0], competitor_words=900,
                                                    conversions=0, clicks=40)}
    assert {"title_query", "description_missing", "h1_query", "thin", "low_ctr", "striking_distance",
            "no_conversions"} <= set(found)
    assert "gutter cleaning near me" in found["striking_distance"]["detail"]
    assert "example co" not in found["striking_distance"]["detail"]  # position 1


def test_primary_query_skips_brand_terms():
    queries = audit.to_queries([row("example co", 30, 900, 1.0), row("gutter cleaning", 2, 400, 3.2)])
    assert audit.primary_query(queries, ["Example Co"]).query == "gutter cleaning"
    assert audit.primary_query(audit.to_queries([row("example co", 3, 90, 1.0)]), ["Example Co"]).query == "example co"
    assert audit.primary_query([], ["Example Co"]) is None


def test_run_audit_stores_proposal_report_and_guards_new_numbers(cfg, db, client):
    crawled(cfg, db, client)
    llm = StubLLM(PROPOSAL)
    aid = audit.run_audit(cfg, db, client, llm, url=SERVICE, log=lambda *_: None, query_rows=rows_for)
    a = db.audit(aid)
    assert a["status"] == "open" and a["primary_query"] == "gutter cleaning"
    proposal = json.loads(a["proposal"])
    assert len(proposal["title"]) <= 60 and proposal["h1"] == "Gutter cleaning service"
    assert any("45" in w for w in proposal["warnings"])  # not stated on the page
    assert not any("149" in w for w in proposal["warnings"])  # the page says $149
    assert "gutter cleaning" in llm.prompts[0] and "$149" in llm.prompts[0]
    report = (cfg.output_dir / "audits" / f"audit-{aid}-gutter-cleaning.md").read_text()
    assert "Request indexing" in report and "How long does a visit take?" in report
    assert "| gutter cleaning | 2 | 400 |" in report


def test_second_audit_reports_change_since_the_applied_one(cfg, db, client):
    crawled(cfg, db, client)
    llm = StubLLM(PROPOSAL)
    first = audit.run_audit(cfg, db, client, llm, url=SERVICE, log=lambda *_: None, query_rows=rows_for)
    audit.mark_done(cfg, db, client, first, log=lambda *_: None)
    later = lambda url: [row("gutter cleaning", 20, 500, 2.1)]  # noqa: E731
    lines = []
    second = audit.run_audit(cfg, db, client, llm, url=SERVICE, log=lines.append, query_rows=later)
    report = json.loads(db.audit(second)["report"])
    assert report["since"]["audit_id"] == first and report["since"]["clicks"] == [2, 20]
    assert any("clicks 2 -> 20" in line for line in lines)


def test_mark_done_pings_indexnow_and_says_to_request_indexing(cfg, db, client, web):
    crawled(cfg, db, client)
    aid = audit.run_audit(cfg, db, client, StubLLM(PROPOSAL), url=SERVICE, log=lambda *_: None, query_rows=rows_for)
    cfg.indexnow.enabled, cfg.indexnow.key = True, "abc123"
    lines = []
    audit.mark_done(cfg, db, client, aid, log=lines.append)
    assert db.audit(aid)["status"] == "applied"
    assert any(r.url.host == "api.indexnow.org" for r in web.requests)
    assert any("Request indexing" in line and SERVICE in line for line in lines)


def test_daily_audit_runs_once_a_day_and_never_fails_the_run(cfg, db, client):
    crawled(cfg, db, client)
    llm = StubLLM(PROPOSAL)
    assert audit.daily_audit(cfg, db, client, llm, log=lambda *_: None, query_rows=rows_for) is not None
    assert audit.daily_audit(cfg, db, client, llm, log=lambda *_: None, query_rows=rows_for) is None  # same day

    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("model down")

    db.set(audit.LAST_KEY, (NOW - timedelta(days=2)).isoformat())
    lines = []
    assert audit.daily_audit(cfg, db, client, Broken(), log=lines.append, query_rows=rows_for) is None
    assert any("Core-page audit failed" in line for line in lines)
    cfg.audit.enabled = False
    db.set(audit.LAST_KEY, (NOW - timedelta(days=2)).isoformat())
    assert audit.daily_audit(cfg, db, client, llm, log=lambda *_: None, query_rows=rows_for) is None


def test_run_once_includes_the_daily_audit(cfg, db, client, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "daily_audit", lambda *a, **k: calls.append(1))
    from autopilot.llm import make_llm

    pipeline.run_once(cfg, db, client, make_llm(cfg.llm, client), publish=False, echo=None)
    assert calls == [1]


def test_cli_audits_list_show_done_dismiss(cfg, db, client, capsys):
    from autopilot import cli

    crawled(cfg, db, client)
    first = audit.run_audit(cfg, db, client, StubLLM(PROPOSAL), url=SERVICE, log=lambda *_: None, query_rows=rows_for)
    second = audit.run_audit(cfg, db, client, StubLLM(PROPOSAL), url=HOME, log=lambda *_: None, query_rows=rows_for)
    config = str(cfg.root / "autopilot.toml")
    cli.main(["--config", config, "audits", "list"])
    out = capsys.readouterr().out
    assert "gutter cleaning" in out and "Next due" not in out  # both pages were just audited
    cli.main(["--config", config, "audits", "show", str(first)])
    assert "Request indexing" in capsys.readouterr().out
    cli.main(["--config", config, "audits", "dismiss", str(second)])
    check = type(db)(cfg.db_path)
    assert check.audit(second)["status"] == "dismissed"
    check.close()


def test_number_guard_ignores_counts_in_the_instructions():
    proposal = {"title": "", "description": "", "h1": "", "edits": [
        {"change": "Add 2-3 sentences and 5 answered questions; say visits take 45 minutes"},
        {"change": "Expand into 1-2 sentences"}]}
    warnings = audit.unsupported_numbers(proposal, "From $149 per visit.")
    assert len(warnings) == 1 and "45" in warnings[0]


def test_live_page_reads_line_breaks_as_spaces(client, monkeypatch):
    html = "<html><head><title>T</title></head><body><h1>Building Leaders<br>On the Mat</h1></body></html>"
    monkeypatch.setattr(audit, "fetch_html", lambda c, u: html)
    page, _ = audit.live_page(client, "https://example.com/breaks/", {}, [])
    assert page["h1"] == "Building Leaders On the Mat"


def test_live_page_keeps_short_stat_blocks_for_the_model(client, monkeypatch):
    html = ("<html><body><main><h1>Brookvale</h1><p>" + "Long paragraph about the program. " * 30 + "</p>"
            "<div><div>290.5</div><p>CA State Record Points</p></div><script>var x = 99;</script></main></body></html>")
    monkeypatch.setattr(audit, "fetch_html", lambda c, u: html)
    page, _ = audit.live_page(client, "https://example.com/", {}, [])
    assert "290.5" in page["facts"] and "CA State Record Points" in page["facts"] and "99" not in page["facts"]
