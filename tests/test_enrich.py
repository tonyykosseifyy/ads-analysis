"""Timeline, playbook checkpoints, Motion classes, context, offers, concepts and copy tags."""
import json
from datetime import date, timedelta

import pytest

from aca import classify, context, copy_analysis, timeline


def days(spec, start="2026-09-01"):
    """spec: list of (spend, atc, ic, pur) per day -> Meta-like daily rows."""
    d0 = date.fromisoformat(start)
    rows = []
    for i, (spend, atc, ic, pur) in enumerate(spec):
        rows.append({"ad_id": "a", "date_start": (d0 + timedelta(days=i)).isoformat(), "spend": str(spend),
                     "impressions": str(int(spend * 200)), "inline_link_clicks": str(int(spend * 3)),
                     "actions": [{"action_type": "omni_add_to_cart", "value": str(atc)},
                                 {"action_type": "omni_initiated_checkout", "value": str(ic)},
                                 {"action_type": "omni_purchase", "value": str(pur)},
                                 {"action_type": "video_view", "value": str(int(spend * 60))}],
                     "action_values": [{"action_type": "omni_purchase", "value": str(pur * 60)}]})
    return timeline.group_daily(rows)["a"]


# ---------------- playbook checkpoints (Account Audit rules) ----------------
def verdict(spec):
    return timeline.playbook_checkpoints(days(spec))["playbook_verdict"]


def test_stop_at_12_only_when_nothing_at_all():
    assert verdict([(12, 0, 0, 0), (12, 0, 0, 0)]) == "stop at $12: nothing at all"
    # juliavieirahair - Silent: 3 ATC and 0 checkouts at $12 must NOT be stopped
    assert verdict([(12, 3, 0, 0), (13, 0, 0, 0)]) != "stop at $12: nothing at all"


def test_stop_at_25_without_checkout_or_sale():
    assert verdict([(12, 1, 0, 0), (13, 1, 0, 0), (10, 0, 0, 0)]) == "stop at $25: no checkout, no sale"


def test_stop_at_45_without_sale_even_with_busy_funnel():
    # "Let's Blowout this hair": lots of ATC/IC, still no sale -> stop
    assert verdict([(12, 3, 2, 0), (13, 4, 3, 0), (22, 4, 3, 0)]) == "stop at $45: no sale"


def test_graduate_with_3_sales_by_55_and_two_sales_rule():
    assert verdict([(12, 2, 1, 1), (13, 1, 1, 1), (20, 1, 1, 0), (12, 1, 1, 1)]) == "graduate: 3+ sales by ~$55"
    assert verdict([(12, 1, 1, 1), (13, 0, 1, 0), (20, 0, 0, 0), (12, 1, 1, 1), (10, 0, 0, 0)]) == \
        "in test: 2 sales, run to $75"
    assert verdict([(12, 1, 1, 1), (13, 0, 1, 0), (20, 0, 0, 0), (12, 1, 1, 1), (20, 1, 1, 1)]) == \
        "graduate: 3 sales by $75"
    assert verdict([(12, 1, 1, 1), (13, 0, 1, 0), (20, 0, 0, 0), (12, 1, 1, 1), (20, 0, 0, 0)]) == \
        "stop at $75: under 3 sales"


def test_too_early_and_in_progress():
    assert verdict([(5, 0, 0, 0)]) == "too early (<$12 spent)"
    assert verdict([(14, 1, 0, 0)]) == "in test: passed $12"


# ---------------- timeline ----------------
def test_timeline_windows_seasons_and_sufficiency():
    spec = [(10, 1, 1, 0)] * 6 + [(10, 2, 2, 1)] * 14
    t = timeline.ad_timeline(days(spec, start="2026-11-15"))  # 13 days in Black Friday, 7 outside
    assert t["first_spend_date"] == "2026-11-15" and t["last_spend_date"] == "2026-12-04"
    assert t["days_with_spend"] == 20 and t["days_live"] == 20
    assert t["first7_spend"] == 70 and t["first7_purchases"] == 1 and t["first7_cpa"] == 70
    assert t["first50_spend"] == 50 and t["days_to_spend_50"] == 5
    assert t["primary_season"] == "Black Friday"
    assert "Black Friday" in t["season_spend_share"] and "regular" in t["season_spend_share"]
    assert t["data_sufficiency"] == "enough data"
    assert "2026-11" in t["months_live"] and "2026-12" in t["months_live"]
    assert timeline.data_sufficiency({"spend": 20, "pur": 1}) == "not enough data (<1 CPA spent)"
    assert timeline.data_sufficiency({"spend": 80, "pur": 2}) == "not enough data (<3 sales)"


def test_fatigue_detected_after_ad_stops_converting():
    spec = [(20, 2, 2, 1)] * 10 + [(20, 1, 0, 0)] * 10
    f = timeline.fatigue(days(spec))
    assert f["days_to_fatigue"] != "" and 10 <= f["days_to_fatigue"] <= 14


def test_monthly_funnels_split_by_product_and_stage():
    d = days([(10, 2, 1, 1)] * 3, start="2026-09-29")
    rows = timeline.monthly_funnels({"a": d}, {"a": {"product": "Blowout Boost", "market": "LB", "funnel_stage": "cold (TOF)"}})
    assert [r["month"] for r in rows] == ["2026-09", "2026-10"]
    sep = rows[0]
    assert sep["spend"] == 20 and sep["purchases"] == 2 and sep["atc_to_ic_pct"] == 50.0 and sep["cpa"] == 10.0


# ---------------- Motion classes ----------------
def test_motion_threshold_and_classes():
    median, threshold = classify.motion_threshold([5, 10, 20, 0, 40])
    assert median == 15 and threshold == 500  # 10x median = 150 -> the $500 floor applies
    _, big = classify.motion_threshold([100, 120, 140])
    assert big == 1200
    assert classify.motion_class(600, 10, "2026-01-01", "2026-11-27", 500) == "winner"
    assert classify.motion_class(300, 30, "2026-01-01", "2026-11-27", 500) == "mid-range"
    assert classify.motion_class(300, 10, "2026-11-26", "2026-11-27", 500) == "too new (still running, <28 days)"
    assert classify.motion_class(300, 10, "2026-03-01", "2026-11-27", 500) == "loser"


# ---------------- context ----------------
def test_campaign_roles_from_real_names():
    cases = {"Prospecting Testing": "testing", "#17 S&S ABO": "testing", "TOF | ABO | Test | Sales | 20 Feb": "testing",
             "Prospecting": "prospecting", "Prospecting | Turblow Pro": "prospecting", "Prospecting_JO": "prospecting",
             "Retargeting - OLD": "retargeting", "BOF | Sales Campaign | 10/feb/2026": "retargeting",
             "Catalog Retargeting": "catalog_retargeting", "Prospecting - Catalog Advantage +": "catalog_prospecting",
             "LLA | Sales campaign | 4 Feb": "prospecting_lookalike", "Retention": "retention",
             "Prospecting_Next_Location": "new_market_test", "CBO | Sales campaign scaling | 9/dec/2025": "prospecting"}
    for name, role in cases.items():
        assert context.campaign_role(name) == role, name
    assert context.campaign_role("Influencer Reach Campaign | Tony", "OUTCOME_AWARENESS") == "awareness"
    assert context.campaign_role("Engagement", "OUTCOME_ENGAGEMENT") == "engagement"


def test_audience_types_and_web_visitor_exclusion():
    broad = context.audience({"geo_locations": {"countries": ["LB"]}, "age_min": 18, "age_max": 65,
                              "excluded_custom_audiences": [{"id": "1", "name": "Web Visitors 30"},
                                                            {"id": "2", "name": "Purchasers 180 days"}],
                              "targeting_automation": {"advantage_audience": 1}})
    assert broad["audience_type"] == "broad" and broad["excludes_web_visitors"] and broad["advantage_audience"]
    warm = context.audience({"custom_audiences": [{"id": "3", "name": "ATC 90"}, {"id": "4", "name": "IC 90"}]})
    assert warm["audience_type"] == "warm_custom_audience"
    assert context.funnel_stage("testing", warm["audience_type"]) == "warm (MOF/BOF)"  # "second chance on warm"
    interest = context.audience({"flexible_spec": [{"interests": [{"id": "5", "name": "Hair"}]}], "genders": [2]})
    assert interest["audience_type"] == "interests" and interest["genders"] == "women" and interest["interests"] == "Hair"
    lla = context.audience({"custom_audiences": [{"id": "6", "name": "Lookalike (LB, 1%) - Purchasers"}]})
    assert lla["audience_type"] == "lookalike"
    assert context.market_from_campaign("Prospecting_JO", "", "") == "JO"


# ---------------- origin, creator, offer ----------------
@pytest.mark.parametrize("name,creator", [
    ("Blowout Boost - Hair dresser - farjine kif btaemliya - Magalie", "Magalie"),
    ("Blowout Boost - Samar Zinati Hook 3 - LB Video", ""),  # creator is inside the hook segment, not its own part
    ("Whirlwind Curl - This hack lezm kel benet taerf aana - Tatiana", "Tatiana"),
    ("Blowout Boost - US Video - Tutorial - Silent", ""),
    ("Blowout Boost - layko l faree ben hal l mayle w ha l mayle - Variation 3 - Pamela Salem", "Pamela Salem"),
])
def test_creator_from_naming_convention(name, creator):
    assert classify.creator_from_name(name) == creator


def test_content_origin():
    assert classify.content_origin("Blowout Boost - Short Hair US", {}) == "US WavyTalk content"
    assert classify.content_origin("Whirlwind - Black Friday - Static", {}) == "static design"
    assert classify.content_origin("Magalie - Hair Dresser", {"language": {"arabic_dialect": "Lebanese"}}) == "Lebanese creator"
    assert classify.is_hairdresser("Magalie - Hair Dresser", {})


def test_offer_from_copy_and_video():
    o = classify.extract_offer(["Get yours this Black Friday before the sale ends! Use ABIGDEAL", "20% OFF NOW - While Stock Lasts"],
                               {"offer": "25% off with 30-day returns", "discount_code": ""})
    assert o["discount_pct"] == 25 and o["discount_code"] == "ABIGDEAL" and o["urgency"]
    assert o["occasion"] == "Black Friday" and o["offer_shown_in"] == "video + text"
    none = classify.extract_offer(["Perfect curls. Zero effort."], {})
    assert none["offer_shown_in"] == "none" and none["discount_pct"] == ""


def test_concepts_group_hook_variations_and_same_body():
    body = [{"start_s": 5 + i, "text_original": t} for i, t in enumerate(
        "this brush gives volume and bounce without heat damage and takes only ten minutes every single morning for me".split())]
    creatives = [
        {"key": "v1", "ad_names": ["Blowout Boost - Samar Zinati Hook 1 - LB Video"], "product": "Blowout Boost", "analysis": {}},
        {"key": "v2", "ad_names": ["Blowout Boost - Samar Zinati Hook 2 - LB Video"], "product": "Blowout Boost", "analysis": {}},
        {"key": "v3", "ad_names": ["Different name A"], "product": "Whirlwind Curl", "analysis": {"transcript": body}},
        {"key": "v4", "ad_names": ["Different name B"], "product": "Whirlwind Curl",
         "analysis": {"transcript": [{"start_s": 0, "text_original": "new hook"}] + body}},
        {"key": "v5", "ad_names": ["Unrelated"], "product": "Whirlwind Curl", "analysis": {}},
    ]
    c = classify.concepts(creatives)
    assert c["v1"] == c["v2"] and c["v3"] == c["v4"] and c["v5"] not in (c["v1"], c["v3"])


def test_links():
    l = classify.links("123", "999", {"effective_object_story_id": "803_122"}, {"permalink": "/WavyTalkLB/videos/1"})
    assert l["ads_manager_link"].endswith("act=999&selected_ad_ids=123")
    assert l["post_link"] == "https://www.facebook.com/803_122"
    assert l["video_link"] == "https://www.facebook.com/WavyTalkLB/videos/1"


# ---------------- copy tags ----------------
class FakeTextAnalyzer:
    def __init__(self):
        self.calls = 0

    def analyze_text(self, prompt, schema, label=""):
        self.calls += 1
        assert "PRIMARY TEXTS" in prompt
        return {"language": "en", "main_angle": "saves_time", "secondary_angles": [], "opening_line": "x",
                "opening_style": "bold_claim", "mentions_offer": True, "offer": "20% off", "social_proof": "",
                "cta": "Shop now", "tone": "casual", "variation_count": 2}


def test_copy_tags_cached_and_deduped(tmp_path):
    copy = {"primary_texts": ["Perfect curls 🌀"], "headlines": ["20% off"], "landing_urls": [], "ctas": []}
    ads = [{"copy": copy}, {"copy": dict(copy)}, {"copy": {"primary_texts": [], "headlines": []}}]
    fa = FakeTextAnalyzer()
    tags = copy_analysis.analyze_copies(ads, fa, tmp_path)
    assert fa.calls == 1 and len(tags) == 1
    again = copy_analysis.analyze_copies(ads, FakeTextAnalyzer(), tmp_path)
    assert again == tags  # second run reads the cache
    cols = copy_analysis.copy_columns(copy, tags[copy_analysis.copy_key(copy)])
    assert cols["copy_main_angle"] == "saves_time" and cols["copy_emoji_count"] == 1
