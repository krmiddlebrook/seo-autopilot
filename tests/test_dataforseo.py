import argparse
import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from autopilot.cli import Ctx, ENV_EXAMPLE, cmd_init
from autopilot.config import load_config
from autopilot.dataforseo import keyword_metrics
from autopilot.research import search
from autopilot.topics import refresh_topics


@pytest.fixture
def paid_config(tmp_path, monkeypatch):
    config = tmp_path / "autopilot.toml"
    cmd_init(argparse.Namespace(config=config, url="https://example.com", name="Example", force=False))
    assert (tmp_path / ".env.example").read_text() == ENV_EXAMPLE == (Path(__file__).parents[1] / "env.example").read_text()
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in ("AUTOPILOT_SEARCH_PROVIDER", "AUTOPILOT_METRICS_PROVIDER", "DATAFORSEO_LOGIN",
                 "DATAFORSEO_PASSWORD", "DATAFORSEO_LOCATION_CODE"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "AUTOPILOT_SEARCH_PROVIDER=dataforseo\nAUTOPILOT_METRICS_PROVIDER=dataforseo\n"
        "DATAFORSEO_LOGIN=fixture\nDATAFORSEO_PASSWORD=fixture\nDATAFORSEO_LOCATION_CODE=2826\n"
    )
    return load_config(config)


def envelope(items, code=20000):
    return {"status_code": 20000, "tasks": [{"status_code": code, "result": [{"items": items}]}]}


def test_live_search_from_env(paid_config):
    def handle(req):
        assert req.method == "POST" and str(req.url) == "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
        assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"fixture:fixture").decode()
        assert json.loads(req.content) == [{"keyword": "repair cost", "depth": 10, "language_code": "en", "location_code": 2826}]
        return httpx.Response(200, json=envelope([
            {"type": "organic", "url": "https://example.org", "title": "Costs", "description": "Repair"},
            {"type": "paid", "url": "https://ad.example"},
            {"type": "people_also_ask", "items": [{"title": "How much?"}]},
            {"type": "related_searches", "items": ["local repairs"]},
        ]))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert search(client, paid_config.search, "repair cost") == {
            "results": [{"url": "https://example.org", "title": "Costs", "snippet": "Repair"}],
            "questions": ["How much?"], "related": ["local repairs"]}


def test_paid_metrics_reach_queue(paid_config, db):
    cfg = paid_config
    cfg.topics.seeds = ["gutters"]

    def handle(req):
        if req.url.host == "suggestqueries.google.com":
            return httpx.Response(200, json=["gutters", ["Gutter prices", "roof quotes"]])
        assert str(req.url) == "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live"
        assert json.loads(req.content) == [{"keywords": ["gutter prices", "roof quotes"], "language_code": "en", "location_code": 2826}]
        return httpx.Response(200, json=envelope([
            {"keyword": "gutter prices", "keyword_info": {"search_volume": 500}, "keyword_properties": {"keyword_difficulty": 0}},
            {"keyword": "roof quotes", "keyword_info": None, "keyword_properties": None},
        ]))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert refresh_topics(cfg, db, client, {}) == 2
    top, second = db.topics()
    assert (top["volume"], top["kd"], top["score"]) == (500, 0, 50)
    assert second["volume"] is None


@pytest.mark.parametrize("payload", [{"status_code": 40200}, envelope([], code=40501), {"status_code": 20000, "tasks": []}])
def test_task_errors_do_not_masquerade_as_empty_results(paid_config, payload):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        with pytest.raises(RuntimeError, match="DataForSEO"):
            search(client, paid_config.search, "test")


def test_batch_limit_and_empty_result(paid_config):
    def handle(req):
        assert len(json.loads(req.content)[0]["keywords"]) == 50
        return httpx.Response(200, json=envelope(None))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert keyword_metrics(client, ["A", "a"] + [str(i) for i in range(60)], "en", "gb") == {}


def test_missing_credentials_and_fake_mode(paid_config, monkeypatch):
    monkeypatch.delenv("DATAFORSEO_PASSWORD")
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail("unexpected request"))) as client:
        with pytest.raises(RuntimeError, match="DATAFORSEO_PASSWORD"):
            search(client, paid_config.search, "test")
    ctx = Ctx(argparse.Namespace(config=paid_config.root / "autopilot.toml", fake=True))
    assert ctx.cfg.search.provider == ctx.cfg.topics.metrics_provider == "none"
    ctx.client.close()
