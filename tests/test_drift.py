import uuid

from autopilot import drift, pipeline
from autopilot.drift import Change, diff_page, quotes

from conftest import PAGES, SITE

MONEY = SITE + "/services/gutter-cleaning/"
OLD = ("Gutter cleaning for homeowners in Brookvale. A standard gutter cleaning visit costs $375 per home. "
       "Crews work Tuesdays and Thursdays from 8am to 4pm. We also offer a spring roof inspection program.")


def money(text, url=MONEY):
    return {"url": url, "kind": "money", "text": text, "title": "Gutters"}


def article(db, markdown, status="published", **kw):
    return db.insert_article({"uid": uuid.uuid4().hex, "slug": uuid.uuid4().hex[:8], "h1": "Gutter guide",
                              "keyword": "gutter cleaning", "markdown": markdown, "html": "", "status": status, **kw})


def test_price_change_is_a_changed_fact():
    [ch] = diff_page(MONEY, OLD, OLD.replace("$375", "$400"))
    assert ch.kind == "changed" and ch.stale == ["$375"] and ch.fresh == ["$400"]
    assert "$400" in ch.new_fact


def test_schedule_change():
    new = OLD.replace("Tuesdays and Thursdays", "Mondays and Thursdays")
    [ch] = diff_page(MONEY, OLD, new)
    assert ch.stale == ["tuesday"] and ch.fresh == ["monday"]


def test_removed_fact_and_unchanged_page():
    new = OLD.replace(" Crews work Tuesdays and Thursdays from 8am to 4pm.", "")
    [ch] = diff_page(MONEY, OLD, new)
    assert ch.kind == "removed" and set(ch.stale) >= {"tuesday", "8am", "4pm"}
    assert diff_page(MONEY, OLD, OLD) == []


def test_quotes_need_value_and_context():
    ch = Change(MONEY, "changed", "A standard gutter cleaning visit costs $375 per home.", "", ["$375"], ["$400"])
    md = ("## Cost\n\nA **gutter cleaning** visit runs [$375](/services/gutter-cleaning/) per home.\n\n"
          "Unrelated: our $375 gift card sale.\n\nWe charge 375 dollars for nothing here.")
    hits = quotes(md, ch)
    assert hits == ["A gutter cleaning visit runs $375 per home."]


def test_compare_flags_articles_and_holds_approved_drafts(db):
    pub = article(db, "Expect to pay about $375 for a gutter cleaning visit.")
    draft = article(db, "Our crews work Tuesdays, so book early.", status="approved")
    clean = article(db, "Gutters matter for your roof.")
    new = OLD.replace("$375", "$400").replace("Tuesdays and Thursdays", "Mondays and Thursdays")
    rep = drift.compare(db, {MONEY: money(OLD)}, [money(new)])
    assert rep.compared == 1 and rep.money_before == 1 and rep.complete
    assert rep.articles_scanned == 3 and len(rep.changes) == 2
    issues = db.fact_issues()
    assert {i["article_id"] for i in issues} == {pub, draft}
    assert db.article(draft)["status"] == "pending_review" and rep.held == [draft]
    assert db.article(clean)["status"] == "published"
    # the same change seen again adds nothing
    again = drift.compare(db, {MONEY: money(OLD)}, [money(new)])
    assert again.flagged == [] and len(db.fact_issues()) == 2
    assert db.get(drift.LAST_KEY)["summary"].startswith("Fact check: compared 1/1")


def test_page_gone_only_when_confirmed(db):
    a = article(db, "A gutter cleaning visit costs $375 per home.")
    rep = drift.compare(db, {MONEY: money(OLD)}, [], gone=lambda u: False)
    assert rep.compared == 0 and rep.skipped and not db.fact_issues()
    assert rep.complete  # skipped pages are counted, never silently dropped
    rep = drift.compare(db, {MONEY: money(OLD)}, [], gone=lambda u: True)
    assert rep.compared == 1 and any(c.kind == "page_gone" for c in rep.changes)
    assert db.fact_issues()[0]["article_id"] == a


def test_text_collapse_is_skipped_not_reported_as_changes(db):
    article(db, "A gutter cleaning visit costs $375 per home.")
    rep = drift.compare(db, {MONEY: money(OLD)}, [money("Loading...")])
    assert rep.compared == 0 and "rendering failure" in rep.skipped[0]["reason"]
    assert not rep.changes and not db.fact_issues()


def test_first_crawl_reports_missing_baseline(db):
    rep = drift.compare(db, {}, [money(OLD)])
    assert rep.money_before == 0 and "baseline" in rep.missing[0]


def test_fix_swaps_value_in_unpublished_article(cfg, db, client):
    aid = article(db, "## Cost\n\nA **gutter cleaning** visit costs $375 per home, cash or card.\n", status="approved")
    drift.compare(db, {MONEY: money(OLD)}, [money(OLD.replace("$375", "$400"))])
    [issue] = db.fact_issues()
    assert pipeline.fix_fact_issue(cfg, db, client, issue["id"], None, log=lambda m: None) == "fixed"
    md = db.article(aid)["markdown"]
    assert "**gutter cleaning** visit costs $400 per home" in md and "$375" not in md
    assert "$400" in db.article(aid)["html"]


def test_fix_removes_sentence_when_fact_is_gone(cfg, db, client):
    aid = article(db, "Gutters fill fast. Our crews clean gutters Tuesdays from 8am. Book early.", status="pending_review")
    new = OLD.replace(" Crews work Tuesdays and Thursdays from 8am to 4pm.", "")
    drift.compare(db, {MONEY: money(OLD)}, [money(new)])
    [issue] = db.fact_issues()
    pipeline.fix_fact_issue(cfg, db, client, issue["id"], None, log=lambda m: None)
    assert db.article(aid)["markdown"] == "Gutters fill fast. Book early."


def test_fix_on_cms_post_is_patched_until_resolved(cfg, db, client):
    aid = article(db, "A gutter cleaning visit costs $375.", remote_url=SITE + "/post/")
    drift.compare(db, {MONEY: money(OLD)}, [money(OLD.replace("$375", "$400"))])
    [issue] = db.fact_issues()
    assert pipeline.fix_fact_issue(cfg, db, client, issue["id"], None, log=lambda m: None) == "patched"
    assert "edit it there" in db.fact_issue(issue["id"])["note"]


def test_fix_on_markdown_target_rewrites_the_file(cfg, db, client, tmp_path):
    cfg.publish.target = "markdown"
    path = tmp_path / "post.mdx"
    path.write_text("---\ntitle: x\n---\nA gutter cleaning visit costs $375.\n")
    article(db, "A gutter cleaning visit costs $375.", remote_id=str(path))
    drift.compare(db, {MONEY: money(OLD)}, [money(OLD.replace("$375", "$400"))])
    [issue] = db.fact_issues()
    assert pipeline.fix_fact_issue(cfg, db, client, issue["id"], None, log=lambda m: None) == "fixed"
    assert path.read_text().endswith("costs $400.\n")


def test_recrawl_flags_article_end_to_end(cfg, db, client, monkeypatch):
    pipeline.crawl(cfg, db, client, log=lambda m: None)
    body = PAGES["/services/gutter-cleaning/"][1]
    monkeypatch.setitem(PAGES, "/services/gutter-cleaning/",
                        ("Gutter Cleaning Service", body + "<p>A gutter cleaning visit costs $375 per home.</p>"))
    pipeline.crawl(cfg, db, client, log=lambda m: None)  # baseline with the price
    aid = article(db, "A gutter cleaning visit from Example Co costs $375.")
    monkeypatch.setitem(PAGES, "/services/gutter-cleaning/",
                        ("Gutter Cleaning Service", body + "<p>A gutter cleaning visit costs $425 per home.</p>"))
    logs = []
    pipeline.crawl(cfg, db, client, log=logs.append)
    [issue] = db.fact_issues()
    assert issue["article_id"] == aid and issue["new_values"] == ["$425"]
    assert any("facts list" in m for m in logs)
