"""Flat tables (one row per ad, one row per creative) for the Sheet and CSVs."""
from __future__ import annotations

import csv
from pathlib import Path

CELL_LIMIT = 45000  # Google Sheets max is 50,000 characters per cell


def _j(items, sep=" | ") -> str:
    return sep.join(str(i) for i in (items or []) if i not in (None, ""))


def _ts(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    return f"{int(s // 60)}:{s % 60:04.1f}"


def creative_columns(rec: dict | None) -> dict:
    """Key analysis fields, flattened. Empty dict when the creative hasn't been watched yet."""
    if not rec:
        return {}
    a = rec.get("analysis", {})
    lang, hook, st = a.get("language", {}), a.get("hook", {}), a.get("structure", {})
    prod, offer, tal, ae = a.get("product", {}), a.get("offer_and_cta", {}), a.get("talent", {}), a.get("audio_and_edit", {})
    share = lang.get("approx_share_pct", {})
    return {
        "summary": a.get("one_line_summary", ""),
        "format": st.get("format", ""),
        "hook_type": hook.get("hook_type", ""),
        "hook_type_2": hook.get("hook_type_secondary", ""),
        "hook_first_frame": hook.get("first_frame", ""),
        "hook_visual_0_3s": hook.get("visual_0_3s", ""),
        "hook_spoken_0_3s": hook.get("spoken_0_3s", ""),
        "hook_text_0_3s": hook.get("text_0_3s", ""),
        "main_angle": st.get("main_angle", ""),
        "secondary_angles": _j(st.get("secondary_angles")),
        "primary_language": lang.get("primary_spoken_language", ""),
        "arabic_pct": share.get("ar", ""),
        "english_pct": share.get("en", ""),
        "french_pct": share.get("fr", ""),
        "code_switching": lang.get("code_switching", ""),
        "dialect": lang.get("arabic_dialect", ""),
        "on_screen_text_langs": _j(lang.get("on_screen_text_languages")),
        "talent_type": tal.get("talent_type", ""),
        "talent_name": tal.get("name_if_shown", ""),
        "setting": tal.get("setting", ""),
        "hair_before": tal.get("hair_before", ""),
        "hair_after": tal.get("hair_after", ""),
        "audio_type": ae.get("audio_type", ""),
        "production_style": ae.get("production_style", ""),
        "pacing": ae.get("pacing", ""),
        "cut_count": ae.get("cut_count", ""),
        "captions_burned_in": ae.get("captions_burned_in", ""),
        "duration_s": a.get("duration_s", ""),
        "speech_starts_at_s": a.get("speech_starts_at_s", ""),
        "product_first_seen_s": prod.get("first_seen_s", ""),
        "product_on_hair_s": prod.get("first_used_on_hair_s", ""),
        "result_shown_s": prod.get("final_result_shown_s", ""),
        "before_after": prod.get("before_after_shown", ""),
        "demo_clarity": prod.get("demo_clarity", ""),
        "offer": offer.get("offer", ""),
        "price_mentioned": offer.get("price_mentioned", ""),
        "discount_code": offer.get("discount_code", ""),
        "cta_spoken": offer.get("cta_spoken", ""),
        "cta_on_screen": offer.get("cta_on_screen", ""),
        "key_claims": _j(st.get("key_claims")),
        "pain_points": _j(st.get("pain_points")),
        "social_proof": _j(st.get("social_proof")),
        "transcript_original": a.get("transcript_full_original", ""),
        "transcript_english": a.get("transcript_full_english", ""),
        "on_screen_text": _j(f"[{_ts(t.get('start_s'))}] {t.get('text')}" for t in a.get("on_screen_text", [])),
        "scenes": _j((f"[{_ts(s.get('start_s'))}-{_ts(s.get('end_s'))}] {s.get('shot_type')}: {s.get('visual')} — {s.get('action')}"
                      for s in a.get("scenes", [])), sep="\n"),
        "full_description": a.get("full_description", ""),
        "model": rec.get("model", ""),
    }


def ad_rows(ads: list[dict], store) -> list[dict]:
    rows = []
    for ad in ads:
        first = ad["media_keys"][0] if ad["media_keys"] else None
        rec = store.get(first) if first else None
        rows.append({
            "ad_id": ad["ad_id"], "ad_name": ad["ad_name"], "market": ad["market"], "product": ad["product"],
            "campaign": ad["campaign_name"], "adset": ad["adset_name"], "status": ad.get("status", ""),
            "created": (ad.get("created_time") or "")[:10],
            **ad["metrics"],
            "media_keys": _j(ad["media_keys"]), "media_count": len(ad["media_keys"]),
            "analysed": bool(rec),
            "headline": _j(ad["copy"]["headlines"]), "primary_text": _j(ad["copy"]["primary_texts"], sep="\n---\n"),
            "landing_url": _j(ad["copy"]["landing_urls"]),
            **creative_columns(rec),
        })
    return rows


def creative_rows(ads: list[dict], store) -> list[dict]:
    """One row per watched asset, with spend/results summed over every ad that used it."""
    usage: dict[str, dict] = {}
    for ad in ads:
        for key in ad["media_keys"]:
            canon = store.canonical(key)
            u = usage.setdefault(canon, {"ads": [], "spend": 0.0, "purchases": 0, "ic": 0, "impr": 0, "v3": 0, "thru": 0,
                                         "clicks": 0, "markets": set(), "products": set()})
            m = ad["metrics"]
            share = 1 / len(ad["media_keys"])  # rotating assets: split the ad's results evenly (see AssetBreakdown for real split)
            u["ads"].append(ad["ad_name"])
            u["spend"] += m["spend"] * share
            u["purchases"] += m["purchases"] * share
            u["ic"] += m["initiate_checkout"] * share
            u["impr"] += m["impressions"] * share
            u["v3"] += m["video_3s_views"] * share
            u["thru"] += m["thruplays"] * share
            u["clicks"] += m["link_clicks"] * share
            u["markets"].add(ad["market"])
            u["products"].add(ad["product"])
    rows = []
    for key, u in sorted(usage.items(), key=lambda kv: -kv[1]["spend"]):
        rec = store.get(key)
        rows.append({
            "media_key": key, "analysed": bool(rec), "ads_using": len(u["ads"]), "ad_names": _j(u["ads"][:8]),
            "markets": _j(sorted(u["markets"])), "products": _j(sorted(u["products"])),
            "spend": round(u["spend"], 2), "purchases": round(u["purchases"], 1),
            "cpa": round(u["spend"] / u["purchases"], 2) if u["purchases"] else "",
            "cost_per_ic": round(u["spend"] / u["ic"], 2) if u["ic"] else "",
            "link_ctr_pct": round(100 * u["clicks"] / u["impr"], 2) if u["impr"] else "",
            "hook_rate_pct": round(100 * u["v3"] / u["impr"], 2) if u["impr"] else "",
            "hold_rate_pct": round(100 * u["thru"] / u["v3"], 2) if u["v3"] else "",
            **creative_columns(rec),
        })
    return rows


def to_grid(rows: list[dict]) -> list[list]:
    if not rows:
        return [["(no rows)"]]
    header = list(dict.fromkeys(k for r in rows for k in r))
    grid = [header]
    for r in rows:
        grid.append([_cell(r.get(h, "")) for h in header])
    return grid


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return s if len(s) <= CELL_LIMIT else s[:CELL_LIMIT] + " …[truncated, see JSON]"


def write_csv(rows: list[dict], path: Path) -> None:
    grid = to_grid(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel shows Arabic correctly
        csv.writer(fh).writerows(grid)
