"""Relevance filters on what a real SERP feeds in, using synthetic club fixtures."""
from autopilot import planning
from autopilot.links import pick_external_links
from autopilot.refresh import find_gaps
from autopilot.research import Competitor, Research

BODY = "word " * 400


def test_gaps_skip_thin_pages_and_competitor_brand_headings():
    tenth = Competitor("https://www.eastfieldclub.example/brookvale-wrestling/", "Youth Wrestling | Eastfield Club Brookvale",
                       headings=[("h2", "Why choose Eastfield Club Brookvale"), ("h2", "Our coaches"),
                                 ("h2", "Practice schedule"), ("h2", "What to bring")], words=900)
    other = Competitor("https://club.example/youth", "Youth wrestling - Club Example",
                       headings=[("h2", "Practice schedule"), ("h2", "Join us today")], words=600)
    thin = Competitor("https://regional-club.example/brookvale", "Brookvale", headings=[("h2", "What to bring")], words=23)
    blocked = Competitor("https://m.yelp.com/search", "Yelp", words=0)
    r = Research("youth wrestling brookvale", competitors=[tenth, other, thin, blocked])

    gaps = find_gaps(r, ["Welcome"], BODY, 10)
    titles = [g["heading"] if "heading" in g else str(g) for g in gaps]
    joined = " ".join(map(str, gaps)).lower()
    assert "eastfield club" not in joined and "our coaches" not in joined and "join us" not in joined
    assert "what to bring" not in joined  # only one substantive page has it; the 23-word page doesn't count
    assert any("practice schedule" in str(g).lower() for g in gaps), titles


def test_gaps_ignore_a_single_competitor():
    solo = Competitor("https://www.eastfieldclub.example/x", "Kids BJJ | Eastfield Club",
                      headings=[("h2", "Kids classes"), ("h2", "Schedule")], words=900)
    assert not [g for g in find_gaps(Research("youth wrestling", competitors=[solo]), [], BODY, 10)
                if "question" not in str(g).lower()]


def test_site_prices_must_relate_to_the_keyword():
    pages = [
        {"title": "Membership | Brookvale Wrestling", "text": "Monthly membership is $200, or $375 for two kids."},
        {"title": "Camp | Brookvale Wrestling", "text": "Summer camp sessions."},
        {"title": "Elite team | Brookvale Wrestling", "text": "Weigh-ins before every dual."},
    ]
    serp = Research("wrestling weight classes", competitors=[
        Competitor("https://flowrestling.org/a", "High school wrestling weight classes", "106, 113, 120", words=800)])
    draft = "Weight classes run from 106 to 285. Membership is $200, or $375 for two kids."
    v = planning.unique_value(draft, pages, serp)
    assert not v.ok and "$200" not in str(v.signals)

    # a page about the topic still counts, and so does a number next to the keyword on another page
    pages.append({"title": "Weight classes | Brookvale Wrestling", "text": "Our youth weight classes start at 52 lbs."})
    v = planning.unique_value("Youth weight classes start at 52 lbs.", pages, serp)
    assert v.ok and "52" in str(v.signals["site_data"])


SERP = [
    {"url": "https://www.flowrestling.org/articles/6745821-high-school-wrestling-weight-classes",
     "title": "High School Wrestling Weight Classes", "snippet": "The NFHS weight classes for high school wrestling."},
    {"url": "https://usawmembership.com/age_group_weight_chart", "title": "Age Group Weight Chart",
     "snippet": "USA Wrestling youth weight classes by age group."},
    {"url": "https://en.wikipedia.org/wiki/Professional_wrestling_weight_classes",
     "title": "Professional wrestling weight classes - Wikipedia", "snippet": "In professional wrestling, weight classes"},
    {"url": "https://law.lis.virginia.gov/admincode/title18/agency120/chapter40/section411.7/",
     "title": "18VAC120-40-411.7. Weight classes", "snippet": "Combat sports bouts by weight."},
    {"url": "https://www.nfhs.org/sports-resource-content/wrestling-weight-classes/",
     "title": "Wrestling Weight Classes | NFHS", "snippet": "High school wrestling weight classes approved for 2026."},
]


def test_external_links_must_fit_the_topic():
    urls = [l["url"] for l in pick_external_links(SERP, "brookvale.example.com", 3, keyword="wrestling weight classes")]
    assert not any("wikipedia" in u or "virginia" in u for u in urls)
    assert any("nfhs.org" in u for u in urls)
    # without a keyword the old authority-only ranking still applies
    assert any("wikipedia" in l["url"] for l in pick_external_links(SERP, "brookvale.example.com", 3))
