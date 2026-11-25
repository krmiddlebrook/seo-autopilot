import base64
import hashlib
import hmac
import json

from autopilot import indexnow
from autopilot.config import Config
from autopilot.links import ensure_links, find_links, pick_external_links
from autopilot.publish.ghost import ghost_token
from autopilot.research import Research
from autopilot.score import invented_experience, score_article, strip_invented_experience
from autopilot.textutil import extract_json, jaccard, slugify
from autopilot.topics import is_duplicate, striking_distance
from autopilot.writer import normalize_outline


def test_slugify():
    assert slugify("Café Guide: How to Clean Gutters (2026)!") == "cafe-guide-how-to-clean-gutters-2026"
    assert len(slugify("word " * 40)) <= 60


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('noise {"b": [1, 2]} trailing') == {"b": [1, 2]}


def test_dedupe():
    assert jaccard("gutter cleaning cost", "cost of gutter cleaning") > 0.6
    assert is_duplicate("how much does gutter cleaning cost", ["gutter cleaning cost"], 0.6)
    assert not is_duplicate("roof repair", ["gutter cleaning cost"], 0.6)


def test_striking_distance_filters_position_impressions_and_brand():
    cfg = Config()
    rows = [
        {"keys": ["gutter cleaning cost", "https://example.com/a"], "position": 12.0, "impressions": 300, "clicks": 3},
        {"keys": ["gutter cleaning", "https://example.com/b"], "position": 3.0, "impressions": 900, "clicks": 90},
        {"keys": ["example co reviews", "https://example.com/"], "position": 14.0, "impressions": 80, "clicks": 1},
        {"keys": ["rare query", "https://example.com/c"], "position": 15.0, "impressions": 2, "clicks": 0},
    ]
    found = striking_distance(rows, cfg.topics, ["Example Co"])
    assert [f["keyword"] for f in found] == ["gutter cleaning cost"]
    assert found[0]["ranking_page"] == "https://example.com/a"


def _outline(data):
    return normalize_outline(data, "gutter cleaning cost", {"business_name": "Example Co"}, Config(),
                             Research(keyword="gutter cleaning cost", questions=["How often should gutters be cleaned?"]))


def test_outline_meta_title_differs_from_h1_and_drops_intro_sections():
    o = _outline({"h1": "Gutter Cleaning Cost", "meta_title": "gutter cleaning cost", "slug": "unrelated",
                  "sections": ["Introduction", {"h2": "Average prices"}, "Conclusion", "FAQ"], "faq": []})
    assert o.meta_title.lower() != o.h1.lower()
    assert [s.h2 for s in o.sections] == ["Average prices"]
    assert o.slug == "gutter-cleaning-cost"
    assert o.faq_questions == ["How often should gutters be cleaned?"]


def test_links_find_and_ensure():
    md = "See [our page](/services/x/) and [us](https://www.example.com/a) and [CDC](https://www.cdc.gov/x)."
    internal, external = find_links(md, "https://example.com")
    assert internal == ["/services/x/", "https://www.example.com/a"]
    assert external == ["https://www.cdc.gov/x"]
    out = ensure_links("Body.", [{"url": "https://example.com/b", "anchor": "B"}])
    assert "[B](https://example.com/b)" in out


def test_external_links_prefer_authorities_and_skip_competitors():
    results = [{"url": "https://competitor.com/guide", "title": "Guide"},
               {"url": "https://www.epa.gov/water", "title": "EPA"},
               {"url": "https://en.wikipedia.org/wiki/Rain_gutter", "title": "Wikipedia"},
               {"url": "https://example.com/own", "title": "Own"}]
    picked = pick_external_links(results, "example.com", 2)
    assert {p["url"] for p in picked} == {"https://www.epa.gov/water", "https://en.wikipedia.org/wiki/Rain_gutter"}


def test_score_rewards_complete_article():
    body = ("When you budget for gutter cleaning cost, start here. " + "word " * 1300 +
            "\n\n## Gutter cleaning cost by home size\n\ntext\n\n## Other\n\ntext")
    kw = dict(keyword="gutter cleaning cost", h1="Gutter Cleaning Cost Guide", meta_title="Gutter Cleaning Cost in 2026",
              meta_description="What gutter cleaning cost looks like in 2026, with real price ranges by home size, "
                               "what drives the price up and how to save money.",
              slug="gutter-cleaning-cost", markdown=body, internal_links=4, external_links=2, faq_count=5,
              has_image=True, min_words=1200, want_internal=4, want_external=2, want_faq=True)
    good = score_article(**kw)
    bad = score_article(**{**kw, "internal_links": 0, "has_image": False, "meta_description": "short"})
    assert good.score >= 85
    assert bad.score < good.score
    assert "meta_description_length" in " ".join(bad.failing()) or bad.failing()

    # site uses the H1 as <title>: the H1 is scored, the meta-title checks go away
    h1_only = score_article(**{**kw, "meta_title": kw["h1"], "seo_title": False})
    names = {c.name for c in h1_only.checks}
    assert "h1_length" in names and not names & {"meta_title_differs_from_h1", "keyword_in_meta_title"}
    assert "h1_length" in score_article(**{**kw, "h1": "Gutter cleaning cost " * 4, "seo_title": False}).failing()

    # site makes its own image: no penalty for not having one
    assert score_article(**{**kw, "has_image": False, "image_expected": False}).score == good.score

    told = score_article(**{**kw, "markdown": body + "\n\nI've watched clients halve their bill."})
    assert "no_invented_experience" in told.failing()


def test_invented_experience_is_found_and_stripped():
    md = ("Start here. I have watched lads add 15kg to their bench. Keep going.\n\n"
          "- In my experience, rest matters. Sleep too.\n\n"
          "### Have I seen this work?\n\n"
          "Our clients tell us it works.\n\n"
          "Pick a weight you can lift with good form.")
    assert len(invented_experience(md)) == 3
    out = strip_invented_experience(md)
    assert out == ("Start here. Keep going.\n\n- Sleep too.\n\n### Have I seen this work?\n\n"
                   "Pick a weight you can lift with good form.")
    assert not invented_experience("Most players see gains in six weeks. If you have trained before, start heavier.")


def test_ghost_token_is_valid_hs256():
    secret_hex = "a" * 64
    token = ghost_token(f"keyid:{secret_hex}", now=1_700_000_000)
    head, payload, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(head)))["kid"] == "keyid"
    body = json.loads(base64.urlsafe_b64decode(pad(payload)))
    assert body == {"iat": 1_700_000_000, "exp": 1_700_000_300, "aud": "/admin/"}
    expected = hmac.new(bytes.fromhex(secret_hex), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(pad(sig)) == expected


def test_indexnow_payload_and_key_file(tmp_path):
    key = indexnow.new_key()
    assert len(key) == 32
    body = indexnow.payload(["https://example.com/a/"], key)
    assert body == {"host": "example.com", "key": key, "urlList": ["https://example.com/a/"],
                    "keyLocation": f"https://example.com/{key}.txt"}
    assert indexnow.write_key_file(tmp_path, key).read_text() == key


def test_apply_replacements_strips_banned_characters_everywhere():
    from autopilot.writer import Draft, Outline, Section, apply_replacements

    o = Outline(keyword="k", h1="Fast — cheap", meta_title="t", meta_description="Fast -- cheap", slug="s", excerpt="e",
                sections=[Section(h2="H", markdown="| a | b |\n| --- | ---- |\nx — y")])
    d = Draft(outline=o, intro="one -- two", conclusion="end", faq=[{"q": "Why — now?", "a": "Because --- reasons"}])
    apply_replacements(d, {" — ": ", ", "—": ", ", "--": "-"})
    text = " ".join([o.h1, o.meta_description, o.sections[0].markdown, d.intro, d.faq[0]["q"], d.faq[0]["a"]])
    assert "--" not in text and "—" not in text
    assert o.h1 == "Fast, cheap" and "| - | - |" in o.sections[0].markdown
