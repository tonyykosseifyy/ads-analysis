"""Pure functions: turn raw Meta payloads into media references and metrics. No network here (easy to test)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# First matching action type wins: they overlap, so they must never be summed.
ACTION_PREFERENCE = {
    "video_3s_views": ["video_view"],
    "add_to_cart": ["omni_add_to_cart", "offsite_conversion.fb_pixel_add_to_cart", "add_to_cart"],
    "initiate_checkout": ["omni_initiated_checkout", "offsite_conversion.fb_pixel_initiate_checkout", "initiate_checkout"],
    "purchases": ["omni_purchase", "offsite_conversion.fb_pixel_purchase", "purchase"],
    "landing_page_views": ["landing_page_view", "omni_landing_page_view"],
}

MARKET_BY_DOMAIN = {"wavytalk-lb.com": "LB", "wavytalk-jo.com": "JO", "wavytalk-sy.com": "SY"}


@dataclass
class MediaRef:
    kind: str                 # "video" | "image" | "ig_media"
    id: str                   # video id, image hash, or IG media id
    alt_ids: list[str] = field(default_factory=list)   # other ids for the same content (fallback download)
    image_url: str | None = None
    role: str = "primary"     # "primary" | "rotating" (one of several different assets)
    ig_fallback: str | None = None  # Instagram media id to try if the Meta video file isn't downloadable

    @property
    def key(self) -> str:
        return {"video": "v", "image": "i", "ig_media": "ig"}[self.kind] + "_" + self.id


# ---------------------------------------------------------------------------
# Creative -> media
# ---------------------------------------------------------------------------
def resolve_media(creative: dict) -> list[MediaRef]:
    """Which videos/images does this creative actually show?

    - Placement-customised ads (optimization_type PLACEMENT) carry the same content cut for Feed and
      Stories/Reels: watch one, keep the others as alternates.
    - Flexible / dynamic ads with several *different* assets: watch each one.
    - Plain video/image ads: the single asset.
    """
    afs = creative.get("asset_feed_spec") or {}
    oss = creative.get("object_story_spec") or {}
    placement_only = afs.get("optimization_type") == "PLACEMENT"
    refs: list[MediaRef] = []

    feed_videos = _unique([v.get("video_id") for v in afs.get("videos", [])])
    if feed_videos:
        if placement_only:
            refs.append(MediaRef("video", feed_videos[0], alt_ids=feed_videos[1:]))
        else:
            role = "rotating" if len(feed_videos) > 1 else "primary"
            refs += [MediaRef("video", v, role=role) for v in feed_videos]

    direct_video = creative.get("video_id")
    story_video = (oss.get("video_data") or {}).get("video_id")
    if not refs and (direct_video or story_video):
        primary = direct_video or story_video
        alts = [v for v in [story_video] if v and v != primary]
        refs.append(MediaRef("video", primary, alt_ids=alts))

    # Carousel cards
    for card in (oss.get("link_data") or {}).get("child_attachments", []) or []:
        if card.get("video_id"):
            refs.append(MediaRef("video", card["video_id"], role="rotating"))
        elif card.get("image_hash"):
            refs.append(MediaRef("image", card["image_hash"], image_url=card.get("picture"), role="rotating"))

    if not refs:
        feed_images = _unique([i.get("hash") for i in afs.get("images", [])])
        if feed_images:
            if placement_only:
                refs.append(MediaRef("image", feed_images[0], alt_ids=feed_images[1:]))
            else:
                role = "rotating" if len(feed_images) > 1 else "primary"
                refs += [MediaRef("image", h, role=role) for h in feed_images]

    if not refs:
        img_hash = (oss.get("link_data") or {}).get("image_hash") or (oss.get("photo_data") or {}).get("image_hash") \
            or creative.get("image_hash")
        if img_hash or creative.get("image_url"):
            refs.append(MediaRef("image", img_hash or _url_key(creative["image_url"]),
                                 image_url=creative.get("image_url")))

    ig = creative.get("source_instagram_media_id") or creative.get("effective_instagram_media_id")
    if not refs and ig:
        refs.append(MediaRef("ig_media", ig))
    elif len(refs) == 1 and refs[0].kind == "video" and ig:
        refs[0].ig_fallback = ig
    return refs


def ad_copy(creative: dict) -> dict:
    """Primary text(s), headline(s) and landing URL as the viewer sees them."""
    afs = creative.get("asset_feed_spec") or {}
    oss = creative.get("object_story_spec") or {}
    story = oss.get("video_data") or oss.get("link_data") or oss.get("photo_data") or {}
    bodies = _unique([b.get("text") for b in afs.get("bodies", [])] + [creative.get("body"), story.get("message")])
    titles = _unique([t.get("text") for t in afs.get("titles", [])] + [creative.get("title"), story.get("title"), story.get("name")])
    links = _unique(
        [l.get("website_url") for l in afs.get("link_urls", [])]
        + [story.get("link"), ((story.get("call_to_action") or {}).get("value") or {}).get("link")]
    )
    cta = _unique(list(afs.get("call_to_action_types", [])) + [(story.get("call_to_action") or {}).get("type")])
    return {"primary_texts": bodies, "headlines": titles, "landing_urls": links, "ctas": cta}


def market_for(landing_urls: list[str]) -> str:
    for url in landing_urls:
        host = (urlparse(url).hostname or "").removeprefix("www.")
        if host in MARKET_BY_DOMAIN:
            return MARKET_BY_DOMAIN[host]
    return "unknown"


PRODUCTS = [  # (canonical name, pattern matched against URL handle / ad name / what the video shows)
    ("Blowout Boost", r"blowout[ -]?boost|blow ?out brush|thermal (round )?brush"),
    ("Whirlwind Curl", r"whirlwind|airflow (curler|styler)|air ?flow curl"),
    ("Steambrush Pro", r"steam ?brush|steam ?line|steamline|steam straighten"),
    ("Power Wave", r"power ?wave|waver"),
    ("Turblow Pro", r"turblow|turbo ?blow|diffuser dryer"),
]


def canonical_product(text: str) -> str | None:
    for name, pattern in PRODUCTS:
        if re.search(pattern, text or "", re.I):
            return name
    return None


def product_for(landing_urls: list[str], ad_name: str, shown: list[str] | None = None) -> str:
    for url in landing_urls:
        m = re.search(r"/products/([^/?#]+)", url)
        if m:
            return canonical_product(m.group(1)) or m.group(1).replace("-", " ").title()
    found = canonical_product(ad_name)
    if found:
        return found
    for item in shown or []:
        found = canonical_product(item)
        if found:
            return found
    if any("collections" in u or u.rstrip("/").endswith((".com", ".com/")) for u in landing_urls):
        return "multi-product"
    return "unknown"


# ---------------------------------------------------------------------------
# Insights -> metrics
# ---------------------------------------------------------------------------
def metrics_from_insight(row: dict) -> dict:
    spend = _f(row.get("spend"))
    impressions = _f(row.get("impressions"))
    actions = _action_map(row.get("actions"))
    values = _action_map(row.get("action_values"))

    picked = {name: _pick(actions, types) for name, types in ACTION_PREFERENCE.items()}
    purchase_value = _pick(values, ACTION_PREFERENCE["purchases"])
    thruplays = _sum_actions(row.get("video_thruplay_watched_actions"))
    views3 = picked["video_3s_views"]
    link_clicks = _f(row.get("inline_link_clicks"))

    return {
        "spend": round(spend, 2),
        "impressions": int(impressions),
        "reach": int(_f(row.get("reach"))),
        "frequency": round(_f(row.get("frequency")), 2),
        "cpm": round(_f(row.get("cpm")), 2),
        "link_clicks": int(link_clicks),
        "link_ctr_pct": round(_f(row.get("inline_link_click_ctr")), 2),
        "cpc_link": _div(spend, link_clicks),
        "video_3s_views": int(views3),
        "thruplays": int(thruplays),
        "hook_rate_pct": _pct(views3, impressions),
        "hold_rate_pct": _pct(thruplays, views3),
        "p25": int(_sum_actions(row.get("video_p25_watched_actions"))),
        "p50": int(_sum_actions(row.get("video_p50_watched_actions"))),
        "p75": int(_sum_actions(row.get("video_p75_watched_actions"))),
        "p100": int(_sum_actions(row.get("video_p100_watched_actions"))),
        "avg_watch_s": round(_sum_actions(row.get("video_avg_time_watched_actions")), 1),
        "landing_page_views": int(picked["landing_page_views"]),
        "add_to_cart": int(picked["add_to_cart"]),
        "initiate_checkout": int(picked["initiate_checkout"]),
        "purchases": int(picked["purchases"]),
        "purchase_value": round(purchase_value, 2),
        "cost_per_atc": _div(spend, picked["add_to_cart"]),
        "cost_per_ic": _div(spend, picked["initiate_checkout"]),
        "cpa": _div(spend, picked["purchases"]),
        "roas": _div(purchase_value, spend),
        "date_start": row.get("date_start"),
        "date_stop": row.get("date_stop"),
    }


# ---------------------------------------------------------------------------
def _unique(items) -> list:
    return list(dict.fromkeys(i for i in items if i))


def _url_key(url: str) -> str:
    import hashlib
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _action_map(rows) -> dict[str, float]:
    return {r.get("action_type"): _f(r.get("value")) for r in (rows or [])}


def _pick(actions: dict[str, float], types: list[str]) -> float:
    for t in types:
        if t in actions:
            return actions[t]
    return 0.0


def _sum_actions(rows) -> float:
    return sum(_f(r.get("value")) for r in (rows or []))


def _div(a: float, b: float):
    return round(a / b, 2) if b else None


def _pct(a: float, b: float):
    return round(100 * a / b, 2) if b else None
