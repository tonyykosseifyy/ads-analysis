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


def _media_analysis(store, key):
    rec = store.get(key) if key else None
    return rec, (rec or {}).get("analysis")


def creative_rows(ads: list[dict], store, data_end_date: str = "") -> list[dict]:
    """One row per watched asset, with results summed over every ad that used it, Motion's winner class
    (computed per creative, as Motion does) and its concept / hook-test group."""
    from .classify import concepts, content_origin, is_hairdresser, motion_class, motion_threshold

    usage: dict[str, dict] = {}
    for ad in ads:
        n = max(len(ad["media_keys"]), 1)
        for key in ad["media_keys"]:
            canon = store.canonical(key)
            u = usage.setdefault(canon, {"ads": [], "spend": 0.0, "purchases": 0, "ic": 0, "atc": 0, "impr": 0, "v3": 0,
                                         "thru": 0, "clicks": 0, "value": 0.0, "markets": set(), "products": set(),
                                         "stages": set(), "dates": set(), "first": "", "last": "",
                                         "adset": ad.get("adset_name", ""), "campaign": ad.get("campaign_name", "")})
            m = ad["metrics"]
            share = 1 / n  # rotating assets: split the ad's results evenly (see AssetBreakdown for Meta's real split)
            u["ads"].append(ad["ad_name"])
            for k, mk in (("spend", "spend"), ("purchases", "purchases"), ("ic", "initiate_checkout"),
                          ("atc", "add_to_cart"), ("impr", "impressions"), ("v3", "video_3s_views"),
                          ("thru", "thruplays"), ("clicks", "link_clicks"), ("value", "purchase_value")):
                u[k] += (m.get(mk) or 0) * share
            u["markets"].add(ad["market"])
            u["products"].add(ad["product"])
            u["stages"].add((ad.get("context") or {}).get("funnel_stage", ""))
            tl = ad.get("timeline") or {}
            u["dates"] |= tl.get("_dates", set())
            if tl.get("first_spend_date"):
                u["first"] = min(filter(None, [u["first"], tl["first_spend_date"]]))
                u["last"] = max(u["last"], tl.get("last_spend_date", ""))

    median, threshold = motion_threshold([u["spend"] for u in usage.values()])
    concept_of = concepts([{"key": k, "ad_names": u["ads"], "product": sorted(u["products"])[0] if u["products"] else "",
                            "analysis": _media_analysis(store, k)[1]} for k, u in usage.items()])
    concept_size: dict[str, int] = {}
    for c in concept_of.values():
        concept_size[c] = concept_size.get(c, 0) + 1

    rows = []
    for key, u in sorted(usage.items(), key=lambda kv: -kv[1]["spend"]):
        rec, analysis = _media_analysis(store, key)
        first_name = u["ads"][0] if u["ads"] else ""
        rows.append({
            "media_key": key, "analysed": bool(rec), "ads_using": len(u["ads"]), "ad_names": _j(u["ads"][:8]),
            "markets": _j(sorted(u["markets"])), "products": _j(sorted(u["products"])),
            "funnel_stages": _j(sorted(x for x in u["stages"] if x)),
            "motion_class": motion_class(u["spend"], len(u["dates"]), u["last"], data_end_date, threshold),
            "motion_threshold_usd": threshold, "account_median_creative_spend": median,
            "days_with_spend": len(u["dates"]), "first_spend_date": u["first"], "last_spend_date": u["last"],
            "concept_id": concept_of.get(key, key), "concept_size": concept_size.get(concept_of.get(key, key), 1),
            "content_origin": content_origin(first_name, analysis, u["adset"], u["campaign"]),
            "hairdresser": is_hairdresser(first_name, analysis),
            "spend": round(u["spend"], 2), "purchases": round(u["purchases"], 1),
            "cpa": round(u["spend"] / u["purchases"], 2) if u["purchases"] else "",
            "roas": round(u["value"] / u["spend"], 2) if u["spend"] else "",
            "cost_per_ic": round(u["spend"] / u["ic"], 2) if u["ic"] else "",
            "cost_per_atc": round(u["spend"] / u["atc"], 2) if u["atc"] else "",
            "link_ctr_pct": round(100 * u["clicks"] / u["impr"], 2) if u["impr"] else "",
            "hook_rate_pct": round(100 * u["v3"] / u["impr"], 2) if u["impr"] else "",
            "hold_rate_pct": round(100 * u["thru"] / u["v3"], 2) if u["v3"] else "",
            "data_sufficiency": ("enough data" if u["purchases"] >= 3 else
                                 "not enough data (<3 sales)" if u["spend"] >= 25 else "not enough data (<1 CPA spent)"),
            **creative_columns(rec),
        })
    return rows


def ad_rows(ads: list[dict], store, creatives: list[dict] | None = None) -> list[dict]:
    from .classify import extract_offer
    from .copy_analysis import copy_columns, copy_key
    by_key = {c["media_key"]: c for c in (creatives or [])}
    rows = []
    for ad in ads:
        first = ad["media_keys"][0] if ad["media_keys"] else None
        rec, analysis = _media_analysis(store, first)
        cre = by_key.get(store.canonical(first)) if first else None
        tl = {k: v for k, v in (ad.get("timeline") or {}).items() if not k.startswith("_")}
        copy = ad["copy"]
        rows.append({
            "ad_id": ad["ad_id"], "ad_name": ad["ad_name"], "market": ad["market"], "product": ad["product"],
            "campaign": ad["campaign_name"], "adset": ad["adset_name"], "status": ad.get("status", ""),
            "created": (ad.get("created_time") or "")[:10],
            **(ad.get("context") or {}),
            **ad["metrics"],
            **tl,
            "motion_class": (cre or {}).get("motion_class", ""), "concept_id": (cre or {}).get("concept_id", ""),
            "concept_size": (cre or {}).get("concept_size", ""),
            "content_origin": (cre or {}).get("content_origin", ""), "hairdresser": (cre or {}).get("hairdresser", ""),
            "creator": ad.get("creator", ""),
            **extract_offer(copy["primary_texts"] + copy["headlines"], (analysis or {}).get("offer_and_cta")),
            **copy_columns(copy, (ad.get("copy_tags") or {}).get(copy_key(copy))),
            "media_keys": _j(ad["media_keys"]), "media_count": len(ad["media_keys"]),
            "analysed": bool(rec),
            **(ad.get("links") or {}),
            "headline": _j(copy["headlines"]), "primary_text": _j(copy["primary_texts"], sep="\n---\n"),
            "landing_url": _j(copy["landing_urls"]),
            **creative_columns(rec),
        })
    return rows


def hook_test_rows(creatives: list[dict]) -> list[dict]:
    """Concepts with 2+ versions side by side, ranked by CPA: which hook won when the body stayed the same."""
    groups: dict[str, list[dict]] = {}
    for c in creatives:
        if (c.get("concept_size") or 1) > 1:
            groups.setdefault(c["concept_id"], []).append(c)
    rows = []
    for cid, members in sorted(groups.items(), key=lambda kv: -sum(m["spend"] for m in kv[1])):
        ranked = sorted(members, key=lambda m: (m["cpa"] == "", m["cpa"] if m["cpa"] != "" else 0, -m["spend"]))
        for rank, m in enumerate(ranked, 1):
            rows.append({
                "concept_id": cid, "versions": len(members), "rank_by_cpa": rank, "media_key": m["media_key"],
                "ad_names": m["ad_names"], "products": m["products"],
                "hook_spoken_0_3s": m.get("hook_spoken_0_3s", ""), "hook_text_0_3s": m.get("hook_text_0_3s", ""),
                "hook_visual_0_3s": m.get("hook_visual_0_3s", ""), "hook_type": m.get("hook_type", ""),
                "spend": m["spend"], "purchases": m["purchases"], "cpa": m["cpa"], "cost_per_ic": m["cost_per_ic"],
                "link_ctr_pct": m["link_ctr_pct"], "hook_rate_pct": m["hook_rate_pct"], "hold_rate_pct": m["hold_rate_pct"],
                "motion_class": m["motion_class"], "data_sufficiency": m["data_sufficiency"],
            })
    return rows


def breakdown_rows(raw: list[dict], ads: list[dict], creatives: list[dict], store) -> list[dict]:
    """Audience/placement splits per ad, tagged with the creative's attributes so they can be pivoted
    (e.g. hook type x age). Per-ad splits are small; read them in aggregate."""
    from .parse import metrics_from_insight
    ad_by_id = {a["ad_id"]: a for a in ads}
    cre = {c["media_key"]: c for c in creatives}
    rows = []
    for r in raw:
        ad = ad_by_id.get(r.get("ad_id"))
        if not ad:
            continue
        first = ad["media_keys"][0] if ad["media_keys"] else None
        c = cre.get(store.canonical(first), {}) if first else {}
        m = metrics_from_insight(r)
        rows.append({
            "dimension": r["_dimension"], "value": r["_value"], "ad_id": ad["ad_id"], "ad_name": ad["ad_name"],
            "product": ad["product"], "market": ad["market"],
            "funnel_stage": (ad.get("context") or {}).get("funnel_stage", ""),
            "format": c.get("format", ""), "hook_type": c.get("hook_type", ""), "main_angle": c.get("main_angle", ""),
            "primary_language": c.get("primary_language", ""), "content_origin": c.get("content_origin", ""),
            "motion_class": c.get("motion_class", ""),
            "spend": m["spend"], "impressions": m["impressions"], "reach": m["reach"], "link_clicks": m["link_clicks"],
            "add_to_cart": m["add_to_cart"], "initiate_checkout": m["initiate_checkout"], "purchases": m["purchases"],
            "purchase_value": m["purchase_value"], "cpa": m["cpa"], "cost_per_ic": m["cost_per_ic"], "roas": m["roas"],
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
