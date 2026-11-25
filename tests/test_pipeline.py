import json

from autopilot import pipeline
from autopilot.brand import BRAND_KEY
from autopilot.cli import main
from autopilot.llm import make_llm


def test_run_once_end_to_end_publishes_to_wordpress(cfg, db, client, web, monkeypatch):
    monkeypatch.setenv("WP_RECEIVER_KEY", "test-key")
    cfg.schedule.review = False
    cfg.publish.target = "wordpress"
    cfg.publish.status = "publish"
    db.set("indexnow", {"key": "abc123", "key_location": "https://example.com/abc123.txt"})
    llm = make_llm(cfg.llm, client)

    made = pipeline.run_once(cfg, db, client, llm, echo=None)

    assert len(made) == 1
    a = db.article(made[0])
    assert db.get(BRAND_KEY)["business_name"] == "Example Co"
    assert len(db.pages()) == 4
    assert a["status"] == "published" and a["remote_url"].startswith("https://example.com/")
    assert a["meta_title"].lower() != a["h1"].lower()
    assert "/services/gutter-cleaning/" in a["markdown"] or "/blog/" in a["markdown"]
    schema = json.loads(a["schema_json"])
    assert {n["@type"] for n in schema} >= {"BlogPosting", "FAQPage"}
    sent = next(iter(web.posts.values()))["body"]
    assert sent["status"] == "publish" and sent["soro_article_id"] == a["uid"]
    assert any(r.url.host == "api.indexnow.org" for r in web.requests)
    assert a["indexnow_at"]
    # the topic is consumed and the next run picks a different one
    assert db.topic(a["topic_id"])["status"] == "used"


def test_review_mode_holds_article(cfg, db, client):
    llm = make_llm(cfg.llm, client)
    [aid] = pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning cost", echo=None)
    a = db.article(aid)
    assert a["status"] == "pending_review"
    assert (cfg.output_dir / f"{a['slug']}.html").exists()
    # the keyword map gives the query one page: a second run for it writes nothing new
    assert pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning cost", echo=None) == []
    assert len(db.articles()) == 1


def test_cli_init_and_offline_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--url", "https://example.com", "--name", "Example Co"])
    assert (tmp_path / "autopilot.toml").exists() and (tmp_path / ".env.example").exists()
    main(["schedule", "cron"])
    assert "autopilot" in capsys.readouterr().out
