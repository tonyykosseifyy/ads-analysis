import json
from pathlib import Path

import pytest

from aca.parse import ad_copy, market_for, metrics_from_insight, product_for, resolve_media

C = json.loads((Path(__file__).parent / "fixtures" / "creatives.json").read_text())


def keys(name):
    return [r.key for r in resolve_media(C[name])]


def test_placement_single_video():
    assert keys("placement_single_video") == ["v_971332045441807"]


def test_placement_cuts_are_one_asset_with_alternate():
    refs = resolve_media(C["placement_two_cuts"])
    assert [r.key for r in refs] == ["v_928674400010108"]
    assert refs[0].alt_ids == ["1015595860890186"]
    assert refs[0].ig_fallback == "18045813797749386"


def test_direct_video_prefers_served_id_and_keeps_story_id_as_fallback():
    refs = resolve_media(C["direct_video"])
    assert refs[0].key == "v_1514336283399725"
    assert refs[0].alt_ids == ["2227782794693199"]


def test_image_placement_dedupes_hashes():
    refs = resolve_media(C["jordan_image_placement"])
    assert [r.key for r in refs] == ["i_e2485451dcbd631d4816b836aff03135"]
    assert refs[0].alt_ids == ["0ec0d643bd3309fc2c004295114e86ad"]


def test_static_link_image():
    refs = resolve_media(C["static_link"])
    assert refs[0].key == "i_11310ad57ef7f994eb2c9ed5ac5204d0"
    assert refs[0].image_url == "https://scontent.example/x.png"


def test_dof_text_variations_still_one_video():
    assert keys("dof_video") == ["v_1957876974908392"]


def test_boosted_post_and_ig_only():
    refs = resolve_media(C["boosted_ig_post"])
    assert refs[0].key == "v_3058352717838238" and refs[0].ig_fallback == "18122261458890911"
    assert keys("ig_only") == ["ig_999"]


def test_flexible_rotating_videos_each_watched():
    refs = resolve_media(C["flexible_three_videos"])
    assert [r.key for r in refs] == ["v_a1", "v_a2", "v_a3"]
    assert all(r.role == "rotating" for r in refs)


@pytest.mark.parametrize("name,market,product", [
    ("direct_video", "LB", "Blowout Boost"),
    ("jordan_image_placement", "JO", "Blowout Boost"),
    ("static_link", "LB", "Whirlwind Curl"),
])
def test_market_and_product(name, market, product):
    copy = ad_copy(C[name])
    assert market_for(copy["landing_urls"]) == market
    assert product_for(copy["landing_urls"], "x") == product


def test_product_from_ad_name_when_no_url():
    assert product_for([], "Steamline Pro - Us Video - Silent") == "Steamline Pro"


def test_copy_collects_all_variations():
    copy = ad_copy(C["dof_video"])
    assert copy["headlines"] == ["GET 20% OFF NOW", "1.5M Sold Worldwide"]
    assert copy["landing_urls"] == ["https://wavytalk-jo.com/products/blowout-boost"]


def test_metrics_do_not_double_count_overlapping_actions():
    row = {
        "spend": "250", "impressions": "10000", "inline_link_clicks": "150", "inline_link_click_ctr": "1.5",
        "actions": [
            {"action_type": "video_view", "value": "3000"},
            {"action_type": "omni_initiated_checkout", "value": "20"},
            {"action_type": "offsite_conversion.fb_pixel_initiate_checkout", "value": "20"},
            {"action_type": "omni_purchase", "value": "10"},
            {"action_type": "purchase", "value": "10"},
            {"action_type": "omni_add_to_cart", "value": "40"},
        ],
        "action_values": [{"action_type": "omni_purchase", "value": "900"}],
        "video_thruplay_watched_actions": [{"action_type": "video_view", "value": "600"}],
    }
    m = metrics_from_insight(row)
    assert m["purchases"] == 10 and m["initiate_checkout"] == 20 and m["add_to_cart"] == 40
    assert m["cpa"] == 25.0 and m["cost_per_ic"] == 12.5 and m["roas"] == 3.6
    assert m["hook_rate_pct"] == 30.0 and m["hold_rate_pct"] == 20.0


def test_metrics_zero_safe():
    m = metrics_from_insight({"spend": "5"})
    assert m["cpa"] is None and m["hook_rate_pct"] is None
