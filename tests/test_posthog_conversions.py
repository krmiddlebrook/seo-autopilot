import json
from datetime import date

import httpx
import pytest

from autopilot import indexing

TODAY = date(2026, 11, 9)


class FakePostHog:
    def __init__(self, rows, status=200):
        self.rows, self.status, self.requests = rows, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        cols = ["day", "landing", "channel", "conversions"]
        if self.rows and len(self.rows[0]) == 3:  # rows without a channel, as before the split
            cols = ["day", "landing", "conversions"]
        return httpx.Response(200, json={"columns": cols, "results": self.rows})


def ph_client(fake):
    return httpx.Client(transport=httpx.MockTransport(fake))


@pytest.fixture
def ph_cfg(cfg, monkeypatch):
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    cfg.measure.posthog_project_id = "12345"
    cfg.measure.posthog_site = "brookvale-wrestling"
    cfg.measure.posthog_events = ["contact_form_submitted", "registration_click"]
    return cfg


def test_query_filters_events_site_test_traffic_and_host(ph_cfg):
    q = indexing.posthog_query(ph_cfg, date(2026, 8, 11))
    assert "event IN ('contact_form_submitted', 'registration_click')" in q
    assert "properties.site = 'brookvale-wrestling'" in q
    assert "IN ('example.com', 'www.example.com')" in q
    assert "analytics_test" in q and "properties.landing_page" in q and "session.$entry_pathname" in q
    assert "toDateTime('2026-08-11 00:00:00')" in q and "uniq(distinct_id, event)" in q


@pytest.mark.parametrize("field,value", [("posthog_events", ["x' OR 1=1 --"]), ("posthog_site", "a'b"),
                                         ("posthog_landing_property", "landing page"),
                                         ("posthog_referrer_property", "a'b"), ("posthog_utm_prefix", "x y"),
                                         ("posthog_social_landings", ["/a'b"])])
def test_query_refuses_unsafe_config(ph_cfg, field, value):
    setattr(ph_cfg.measure, field, value)
    with pytest.raises(ValueError):
        indexing.posthog_query(ph_cfg, TODAY)


def test_fetch_stores_rows_by_landing_url_and_channel_and_replaces_window(ph_cfg, db):
    fake = FakePostHog([["2026-11-06", "/", "search", 4], ["2026-11-06", "/", "social", 1],
                        ["2026-11-06", "/juniors", "search", 2], ["2026-11-07", "/", "direct", 2],
                        ["2026-11-08", "https://example.com/juniors", "search", 1],
                        ["2026-11-08", "/juniors", "search", 1], ["2026-11-08", "", "search", 3],
                        ["2026-11-08", "/x", "weird", 1]])
    with ph_client(fake) as client:
        assert indexing.fetch_posthog_conversions(ph_cfg, db, client, 90, today=TODAY) == 6
        req = fake.requests[0]
        assert str(req.url) == "https://us.posthog.com/api/projects/12345/query/"
        assert req.headers["authorization"] == "Bearer phx_test"
        body = json.loads(req.content)
        assert body["query"]["kind"] == "HogQLQuery" and "FROM events" in body["query"]["query"]
        rows = {(r["url"], r["date"], r["source"]): r["count"] for r in db.conversions()}
        assert rows == {("https://example.com/", "2026-11-06", "posthog:search"): 4,
                        ("https://example.com/", "2026-11-06", "posthog:social"): 1,
                        ("https://example.com/juniors", "2026-11-06", "posthog:search"): 2,
                        ("https://example.com/", "2026-11-07", "posthog:direct"): 2,
                        ("https://example.com/juniors", "2026-11-08", "posthog:search"): 2,
                        ("https://example.com/x", "2026-11-08", "posthog:other"): 1}
        # a second pull with fewer rows replaces the window instead of adding to it, old untagged rows included
        db.add_conversions("https://example.com/", "2026-11-05", 9, "posthog")
        fake.rows = [["2026-11-06", "/", "search", 1]]
        indexing.fetch_posthog_conversions(ph_cfg, db, client, 90, today=TODAY)
        assert [(r["source"], r["count"]) for r in db.conversions()] == [("posthog:search", 1)]


def test_query_tags_channels_and_social_landings(ph_cfg):
    ph_cfg.measure.posthog_social_landings = ["/links"]
    q = indexing.posthog_query(ph_cfg, TODAY)
    assert "AS channel" in q and "GROUP BY day, landing, channel" in q
    assert "properties.landing_referrer" in q and "properties.landing_utm_medium" in q
    assert "properties.landing_utm_source" in q and "IN ('/links')" in q
    assert "'search'" in q and "'social'" in q and "'ai'" in q and "'direct'" in q
    assert "\\" not in q  # no backslash escapes for HogQL to misread


def test_fetch_keeps_other_sources(ph_cfg, db):
    db.add_conversions("https://example.com/contact", "2026-11-06", 5, "csv")
    with ph_client(FakePostHog([])) as client:
        indexing.fetch_posthog_conversions(ph_cfg, db, client, 90, today=TODAY)
    assert [r["source"] for r in db.conversions()] == ["csv"]


def test_missing_key_names_the_env_var(ph_cfg, db, monkeypatch):
    monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY")
    with ph_client(FakePostHog([])) as client, pytest.raises(RuntimeError, match="POSTHOG_PERSONAL_API_KEY"):
        indexing.fetch_posthog_conversions(ph_cfg, db, client, 30, today=TODAY)


def test_refused_key_explains_scope(ph_cfg, db):
    with ph_client(FakePostHog([], status=403)) as client, pytest.raises(RuntimeError, match="query:read"):
        indexing.fetch_posthog_conversions(ph_cfg, db, client, 30, today=TODAY)


def test_unconfigured_is_a_no_op(cfg, db):
    with ph_client(FakePostHog([])) as client:
        assert indexing.fetch_posthog_conversions(cfg, db, client, 30, today=TODAY) == 0


def test_report_measures_the_goal_on_search_and_shows_every_channel(ph_cfg, db):
    db.insert_article({"uid": "u1", "keyword": "kw", "slug": "juniors", "h1": "x", "status": "published",
                       "remote_url": "https://example.com/juniors/"})
    db.add_conversions("https://example.com/contact", "2026-11-05", 1, "csv")  # untagged: counts toward the goal
    fake = FakePostHog([["2026-11-06", "/", "search", 4], ["2026-11-06", "/juniors", "search", 2],
                        ["2026-11-06", "/links", "social", 3], ["2026-11-07", "/", "direct", 1]])
    with ph_client(fake) as client:
        rep = indexing.build_report(ph_cfg, db, client, 28, log=lambda *_: None, today=TODAY)
    cv = rep["conversions"]
    assert cv["value"] == 7 and cv["all"] == 11 and cv["sources"] == ["csv", "posthog"]
    assert cv["on_autopilot_pages"] == 2 and cv["by_channel"] == {"search": 6, "social": 3, "direct": 1}
    assert cv["by_url"][0] == {"url": "https://example.com/", "count": 4}
    text = indexing.format_report(rep)
    assert "Conversions from search: 7" in text
    assert "All conversions: 11 (search 6, social 3, direct 1)" in text
    assert "Search conversions by landing page: https://example.com/ 4" in text


def test_report_without_channels_reads_as_before(ph_cfg, db):
    ph_cfg.measure.posthog_project_id = ""
    db.add_conversions("https://example.com/contact", "2026-11-05", 2, "csv")
    rep = indexing.build_report(ph_cfg, db, None, 28, log=lambda *_: None, today=TODAY)
    text = indexing.format_report(rep)
    assert rep["conversions"]["value"] == 2 and "Conversions: 2" in text and "All conversions" not in text


def test_report_notes_posthog_failure(ph_cfg, db, monkeypatch):
    monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY")
    with ph_client(FakePostHog([])) as client:
        rep = indexing.build_report(ph_cfg, db, client, 28, log=lambda *_: None, today=TODAY)
    assert any("PostHog conversions not refreshed" in n and "POSTHOG_PERSONAL_API_KEY" in n for n in rep["notes"])


def test_cli_conversions_posthog(tmp_path, capsys, monkeypatch):
    from autopilot.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    main(["init", "--url", "https://example.com", "--name", "Example Co"])
    toml = tmp_path / "autopilot.toml"
    toml.write_text(toml.read_text().replace("[measure]", '[measure]\nposthog_project_id = "12345"', 1))
    fake = FakePostHog([["2026-11-06", "/contact", "search", 1]])
    real = httpx.Client

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(fake)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "Client", fake_client)
    main(["conversions", "posthog", "--days", "90"])
    main(["conversions"])
    out = capsys.readouterr().out
    assert "Stored 1 landing page/day row(s) from PostHog (contact_form_submitted)" in out
    assert "https://example.com/contact" in out and "posthog" in out
