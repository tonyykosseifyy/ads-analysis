"""Labels that turn raw rows into comparable groups: Motion's winner classes, content origin, creator,
live offer, concept / hook-test groups and one-click links."""
from __future__ import annotations

import re
import statistics
from collections import defaultdict

# ---------------------------------------------------------------------------
# Motion Creative Benchmarks 2026: winner = spend >= 10x the account's median creative spend AND >= $500.
# Mid-range = not a winner, but spent on 28+ days. Loser = stopped before 28 days of spend.
# ---------------------------------------------------------------------------
MOTION_MULTIPLE, MOTION_FLOOR, MOTION_MIDRANGE_DAYS = 10, 500.0, 28


def motion_threshold(creative_spends: list[float]) -> tuple[float, float]:
    spends = [s for s in creative_spends if s > 0]
    median = statistics.median(spends) if spends else 0.0
    return round(median, 2), round(max(MOTION_MULTIPLE * median, MOTION_FLOOR), 2)


def motion_class(spend: float, days_with_spend: int, last_spend_date: str, data_end_date: str,
                 threshold: float) -> str:
    if spend >= threshold:
        return "winner"
    if days_with_spend >= MOTION_MIDRANGE_DAYS:
        return "mid-range"
    if last_spend_date and data_end_date and last_spend_date >= _minus_days(data_end_date, 2):
        return "too new (still running, <28 days)"
    return "loser"


def _minus_days(iso: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Content origin and creator
# ---------------------------------------------------------------------------
TAG_WORDS = re.compile(r"^(us|lb|arabic|silent|voice ?over|static( image)?|copy( \d+)?|variation \d+|version \d+|"
                       r"hook \d+|us video|lb video|video \d*|comment|trend|frame|image|pic|engagement( jordan)?|"
                       r"black friday|reminder image|review image|us influencer)$", re.I)


NOT_NAMES = {"tutorial", "silent", "review", "reaction", "trend", "comment", "influencer", "ugc", "voiceover",
             "hairdresser", "hair dresser", "hair stylist", "short hair us", "short hair", "curly hair", "long hair",
             "multi angle", "static", "image", "picture", "video", "us video", "lb video", "arabic", "copy",
             "engagement", "frame", "pretty girl silent", "coily hair", "orange hair", "blond", "brunette", "mom",
             "black friday", "christmas", "ramadan", "summer", "fall", "testing", "test", "batch"}


NOT_NAME_WORDS = re.compile(r"\b(hair|section|curls?|boost|wave|pro|brush|blowout|styling|styler|look|looks|routine|"
                            r"concept|tool|video|sale|offer|off|static|image|picture|silent|tutorial|whirlwind|"
                            r"steam\w*|turblow|power|bangs|layers|front|back|side|batch|copy)\b", re.I)


def creator_from_name(ad_name: str) -> str:
    """Naming convention is "Product - hook line - creator" plus tags (US Video, Silent, Variation 3...)."""
    parts = [p.strip() for p in re.split(r"\s+-\s+", ad_name or "") if p.strip()]
    for p in reversed(parts[1:]):
        if TAG_WORDS.match(p) or p.lower() in NOT_NAMES or len(p.split()) > 3 or re.search(r"\d{3,}|%", p) \
                or NOT_NAME_WORDS.search(p):
            continue
        if re.match(r"^[A-Z][a-zA-Z]+( [A-Z][a-zA-Z]+){0,2}$", p):
            return p
    return ""


def content_origin(ad_name: str, analysis: dict | None, adset_name: str = "", campaign_name: str = "") -> str:
    a = analysis or {}
    lang = a.get("language", {})
    name = (ad_name or "").lower()
    container = f"{adset_name} {campaign_name}".lower()
    if a.get("asset_type") == "image" or re.search(r"\bstatic\b|\bimage\b|\bpic\b", name):
        return "static design"
    if re.search(r"\bus\b|us video|us influencer", name):
        return "US WavyTalk content"
    if re.search(r"\blb video\b|\barabic\b", name) or "lebanese" in str(lang.get("arabic_dialect", "")).lower():
        return "Lebanese creator"
    if re.search(r"\bus videos?\b", container) and lang.get("primary_spoken_language") not in ("ar", "mixed"):
        return "US WavyTalk content"
    if re.search(r"\b(lb|lebanese|leb) videos?\b|influencer", container):
        return "Lebanese creator"
    if lang.get("primary_spoken_language") in ("ar", "mixed"):
        return "Arabic-speaking creator"
    if a.get("audio_and_edit", {}).get("production_style") == "brand_footage":
        return "brand footage"
    return "English, origin unclear"


def is_hairdresser(ad_name: str, analysis: dict | None) -> bool:
    talent = ((analysis or {}).get("talent") or {}).get("talent_type")
    return talent == "hairdresser_expert" or bool(re.search(r"hair ?dresser|hairstylist|coiffeur", ad_name or "", re.I))


# ---------------------------------------------------------------------------
# Live offer (from the video and from the ad text)
# ---------------------------------------------------------------------------
NOT_CODES = {"SHOP", "NOW", "OFF", "GET", "NEW", "USD", "FREE", "SALE", "WOW", "OMG", "POV", "GRWM", "DIY", "LB", "JO",
             "BLACK", "FRIDAY", "WHILE", "STOCK", "LASTS", "LIMITED", "OFFER", "HAIR", "TIKTOK", "BEST"}


def extract_offer(texts: list[str], video_offer: dict | None = None) -> dict:
    blob = "\n".join(t for t in texts if t)
    pcts = [int(m) for m in re.findall(r"(\d{1,2})\s?%", blob) if 0 < int(m) < 90]
    codes = [c for c in re.findall(r"\b([A-Z][A-Z0-9]{3,14})\b", blob)
             if c not in NOT_CODES and (re.search(r"\d", c) or c.isupper() and len(c) >= 6) and not c.isdigit()]
    v = video_offer or {}
    vtxt = " ".join(str(v.get(k, "")) for k in ("offer", "price_mentioned", "discount_code", "urgency"))
    pcts += [int(m) for m in re.findall(r"(\d{1,2})\s?%", vtxt) if 0 < int(m) < 90]
    if v.get("discount_code"):
        codes.append(str(v["discount_code"]).upper())
    urgency = bool(re.search(r"while stock lasts|limited|ends|last chance|today only|before the sale ends|selling out",
                             blob + " " + vtxt, re.I))
    free_delivery = bool(re.search(r"free (delivery|shipping)|توصيل مجاني", blob + " " + vtxt, re.I))
    occasion = next((o for o in ("Black Friday", "Christmas", "Ramadan", "Valentine", "Mother", "Eid")
                     if re.search(o, blob + " " + vtxt, re.I)), "")
    in_copy = bool(re.search(r"\d{1,2}\s?%|free (delivery|shipping)", blob, re.I)) or bool(codes)
    in_video = bool(v.get("offer") or v.get("discount_code") or v.get("price_mentioned"))
    return {
        "discount_pct": max(pcts) if pcts else "",
        "discount_code": " | ".join(dict.fromkeys(codes)),
        "free_delivery": free_delivery, "urgency": urgency, "occasion": occasion,
        "offer_shown_in": "video + text" if in_copy and in_video else ("video" if in_video else ("text" if in_copy else "none")),
    }


# ---------------------------------------------------------------------------
# Concepts and hook tests
# ---------------------------------------------------------------------------
def name_stem(ad_name: str) -> str:
    s = (ad_name or "").lower()
    s = re.sub(r"\b(hook|variation|version|v)\s*#?\d+\b", " ", s)
    s = re.sub(r"\(batch \d+\)|\bcopy( \d+)?\b|\btesting\b", " ", s)
    s = re.sub(r"[^a-z0-9؀-ۿ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _body_words(analysis: dict | None) -> set[str]:
    segs = (analysis or {}).get("transcript") or []
    words = " ".join(s.get("text_original", "") for s in segs if (s.get("start_s") or 0) >= 4)
    return {w for w in re.findall(r"[\w؀-ۿ]+", words.lower()) if len(w) > 2}


def concepts(creatives: list[dict]) -> dict[str, str]:
    """Group creatives that are the same video with a different hook/edit.

    Two creatives join when they share an ad-name stem (Hook 1/2/3, Variation N, Copy) or when the same
    product's transcripts after the first 4 seconds overlap by half or more (same body, new opening).
    Input dicts need: key, ad_names (list), product, analysis. Returns key -> concept id.
    """
    parent = {c["key"]: c["key"] for c in creatives}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_stem = defaultdict(list)
    for c in creatives:
        for n in c["ad_names"]:
            stem = name_stem(n)
            if len(stem) > 8:
                by_stem[stem].append(c["key"])
    for keys in by_stem.values():
        for k in keys[1:]:
            union(keys[0], k)

    bodies = {c["key"]: (_body_words(c.get("analysis")), c.get("product")) for c in creatives}
    keys = [k for k, (w, _) in bodies.items() if len(w) >= 12]
    for i, a in enumerate(keys):
        wa, pa = bodies[a]
        for b in keys[i + 1:]:
            wb, pb = bodies[b]
            if pa == pb and len(wa & wb) / len(wa | wb) >= 0.5:
                union(a, b)
    return {c["key"]: find(c["key"]) for c in creatives}


# ---------------------------------------------------------------------------
# Links the team can click (stable ones only; Meta CDN thumbnails expire within days)
# ---------------------------------------------------------------------------
def links(ad_id: str, account_id: str, creative: dict, source: dict | None) -> dict:
    story = (creative or {}).get("effective_object_story_id") or ""
    permalink = (source or {}).get("permalink") or ""
    if permalink.startswith("/"):
        permalink = "https://www.facebook.com" + permalink
    return {
        "ads_manager_link": f"https://adsmanager.facebook.com/adsmanager/manage/ads?act={account_id}&selected_ad_ids={ad_id}",
        "post_link": f"https://www.facebook.com/{story}" if story else "",
        "video_link": permalink,
    }
