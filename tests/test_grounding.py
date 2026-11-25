"""Articles may only say what the client's own site says about the business, and only cite sites research returned."""

import json

from autopilot import pipeline
from autopilot.fake import fake_responder
from autopilot.facts import plan_facts, source_text
from autopilot.links import pick_external_links, region_terms, strip_unknown_links
from autopilot.llm import FakeLLM
from autopilot.score import score_article, unsupported_brand_claims, unverified_domains
from autopilot.writer import Outline, Section

CLUB_PAGES = [
    {"url": "https://brookvale.example.com/", "kind": "home", "title": "Brookvale Wrestling",
     "text": "Brookvale Wrestling is a youth wrestling club in Brookvale, California for wrestlers of all ages and skill levels. "
             "Practices run Monday through Thursday at the Brookvale High School wrestling room."},
    {"url": "https://brookvale.example.com/juniors", "kind": "money", "title": "Junior Squad Youth Program",
     "text": "Junior Squad is our youth program for ages 5 to 14. Cost: $375 per season or $125 per month. "
             "Elite training is $200 per month. Register online before the season starts."},
    {"url": "https://brookvale.example.com/club-circle", "kind": "page", "title": "Club Circle",
     "text": "The Club Circle is a paid membership. Member dues fund elite training, coaching clinics and travel "
             "tournaments for our competitive wrestlers."},
]
BRAND = {"business_name": "Brookvale Wrestling", "location": "Brookvale, San Diego County, CA",
         "offerings": [{"name": "Junior Squad", "url": ""}], "cta": "Join at brookvale.example.com/juniors"}
SOURCE = source_text(CLUB_PAGES, BRAND)


def claims(md: str) -> list[str]:
    return unsupported_brand_claims(md, ["Brookvale Wrestling"], SOURCE)


def test_flags_invented_business_facts():
    md = (
        "Brookvale Wrestling offers scholarships and financial aid through the Club Circle foundation.\n\n"
        "Brookvale Wrestling follows a structured curriculum. Registration opens seasonally, and spots fill quickly.\n\n"
        "- Brookvale Wrestling often allows prospective families to observe or try a session.\n\n"
        "Our monthly fee is $150."
    )
    hits = claims(md)
    assert any("scholarships" in h for h in hits)
    assert any("structured curriculum" in h for h in hits)
    assert any("spots fill quickly" in h for h in hits)  # no brand name, but a business fact in a brand paragraph
    assert any("observe" in h for h in hits)
    assert any("$150" in h for h in hits)  # a price the site never states


def test_accepts_facts_the_site_states():
    md = (
        "Brookvale Wrestling's Junior Squad program is for ages 5 to 14 and costs $375 per season or $125 per month.\n\n"
        "The Club Circle is a paid membership that funds elite training at Brookvale Wrestling.\n\n"
        "Contact Brookvale Wrestling directly to confirm current details.\n\n"
        "Most beginners improve fastest with steady practice and good sleep."  # not about the business at all
    )
    assert claims(md) == []


def test_score_article_holds_brand_claims_as_a_failing_check():
    kw = dict(keyword="youth wrestling", h1="Youth Wrestling Guide", meta_title="Youth Wrestling in Brookvale",
              meta_description="x" * 120, slug="youth-wrestling", markdown="Youth wrestling.", internal_links=3,
              external_links=1, faq_count=5, has_image=True, min_words=1, want_internal=3, want_external=1,
              want_faq=True)
    bad = score_article(**kw, brand_claims=["Brookvale Wrestling offers scholarships."], unverified=["madeup.com"])
    assert {"no_unsupported_brand_claims", "no_unverified_domains"} <= set(bad.failing())
    assert "no_unsupported_brand_claims" not in score_article(**kw, brand_claims=[], unverified=[]).failing()


def test_external_links_prefer_national_and_in_region_sources():
    serp = [
        {"url": "https://www.fairfaxcounty.gov/parks/wrestling", "title": "Youth Wrestling | Fairfax County"},
        {"url": "https://www.springville.org/rec/wrestling", "title": "Youth Wrestling - City of Springville Recreation"},
        {"url": "https://www.parks.ut.gov/wrestling", "title": "Utah State Parks"},
        {"url": "https://www.sandiego.gov/park-and-recreation/youth", "title": "Youth Sports | City of San Diego"},
        {"url": "https://www.health.gov.au/kids-sport", "title": "Kids sport | Australian Government"},
        {"url": "https://www.cdc.gov/physical-activity/youth", "title": "Youth Physical Activity | CDC"},
        {"url": "https://www.usawmembership.com/", "title": "USA Wrestling membership"},
    ]
    picked = [l["url"] for l in pick_external_links(serp, "brookvale.example.com", 5, "us", BRAND["location"])]
    assert "https://www.cdc.gov/physical-activity/youth" in picked
    assert "https://www.sandiego.gov/park-and-recreation/youth" in picked
    assert not any(h in u for u in picked for h in ("fairfaxcounty", "springville", "parks.ut.gov", "gov.au"))
    # with no known location, local government pages are dropped altogether
    assert "https://www.sandiego.gov/park-and-recreation/youth" not in [
        l["url"] for l in pick_external_links(serp, "brookvale.example.com", 5, "us", "")]
    assert region_terms("Brookvale, San Diego County, CA") == ["brookvale", "san diego", "ca"]


def test_unknown_domains_are_unlinked_and_flagged():
    allowed = {"brookvale.example.com", "usawmembership.com", "cdc.gov"}
    md = ("Get a card at [USA Wrestling](https://usawrestlingmembership.com/join) or "
          "[the real site](https://www.usawmembership.com/). See [Junior Squad](/juniors) and "
          "[CDC](https://www.cdc.gov/x).")
    fixed, removed = strip_unknown_links(md, allowed)
    assert removed == ["https://usawrestlingmembership.com/join"]
    assert "Get a card at USA Wrestling or [the real site](https://www.usawmembership.com/)" in fixed
    assert unverified_domains(fixed, allowed) == []
    assert unverified_domains("Sign up at usawrestlingmembership.com today.", allowed) == ["usawrestlingmembership.com"]


def test_sections_get_relevant_page_text():
    outline = Outline("youth wrestling cost", "h1", "m", "d", "s", "e",
                      [Section("How much youth wrestling costs"), Section("Elite training and the Club Circle")])
    general = plan_facts(outline, CLUB_PAGES)
    assert "$375 per season" in general and "/juniors" in general
    assert "$375 per season" in outline.sections[0].facts  # the money page always rides along
    assert "paid membership" in outline.sections[1].facts


def test_pipeline_feeds_site_text_and_holds_invented_brand_claims(cfg, db, client):
    cfg.schedule.review = False
    cfg.publish.target = "none"

    def responder(system: str, prompt: str, json_mode: bool = False) -> str:
        out = fake_responder(system, prompt, json_mode)
        if "Write ONLY this section" in prompt and "## Costs and timing" in prompt:
            out += ("\n\nExample Co offers scholarships and financial aid through its community foundation. "
                    "Check [the membership portal](https://madeup-membership.com/join).")
        return out

    llm = FakeLLM(responder)
    [aid] = pipeline.run_once(cfg, db, client, llm, keyword="gutter cleaning cost", echo=None)
    a = db.article(aid)

    section_prompts = [p for _, p, _ in llm.calls if "Write ONLY this section" in p]
    assert section_prompts and all("Facts from Example Co's own website" in p for p in section_prompts)
    assert any("Professional gutter cleaning for homeowners" in p for p in section_prompts)  # pages.text
    assert "may ONLY come from" in llm.calls[-1][0]

    assert a["status"] == "pending_review"  # review is off, so the claim alone held it
    report = json.loads(a["score_report"])
    failing = {c["name"]: c["detail"] for c in report["checks"] if not c["passed"]}
    assert "scholarships" in failing["no_unsupported_brand_claims"]
    assert "madeup-membership.com" not in a["markdown"]
    assert "the membership portal" in a["markdown"]
