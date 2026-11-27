"""Everything that needs day-by-day data: when an ad ran, fair early-window results, the playbook's
stop/keep/graduate checkpoints, fatigue, seasons and monthly funnels per product.

Daily rows come from Meta at day granularity, so a checkpoint such as "$25 spent" is read at the end of the
day the ad crossed $25 (it may have spent a few dollars more by then).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .parse import ACTION_PREFERENCE, _action_map, _f, _pick, _sum_actions

TARGET_CPA = 25.0  # playbook / account target; the checkpoints below scale from it

# Approximate windows; edit as needed. First match wins, so short events sit before long ones.
SEASONS = [
    ("Black Friday", "2025-11-20", "2025-12-02"),
    ("Christmas & New Year", "2025-12-10", "2026-01-06"),
    ("Valentine's", "2026-02-07", "2026-02-14"),
    ("Mother's Day & Eid al-Fitr", "2026-03-14", "2026-03-23"),
    ("Ramadan", "2026-02-18", "2026-03-13"),
    ("Eid al-Adha", "2026-05-25", "2026-05-30"),
    ("Summer", "2026-06-15", "2026-09-15"),
    ("Black Friday", "2026-11-19", "2026-12-01"),
    ("Christmas & New Year", "2026-12-10", "2027-01-06"),
]


def season_of(day: str) -> str:
    for name, start, end in SEASONS:
        if start <= day <= end:
            return name
    return "regular"


def day_record(row: dict) -> dict:
    a, v = _action_map(row.get("actions")), _action_map(row.get("action_values"))
    return {
        "date": row.get("date_start"), "spend": _f(row.get("spend")), "impressions": _f(row.get("impressions")),
        "clicks": _f(row.get("inline_link_clicks")), "v3": _pick(a, ACTION_PREFERENCE["video_3s_views"]),
        "thru": _sum_actions(row.get("video_thruplay_watched_actions")),
        "lpv": _pick(a, ACTION_PREFERENCE["landing_page_views"]), "atc": _pick(a, ACTION_PREFERENCE["add_to_cart"]),
        "ic": _pick(a, ACTION_PREFERENCE["initiate_checkout"]), "pur": _pick(a, ACTION_PREFERENCE["purchases"]),
        "value": _pick(v, ACTION_PREFERENCE["purchases"]),
    }


def group_daily(rows: list[dict]) -> dict[str, list[dict]]:
    by_ad: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rec = day_record(r)
        if rec["date"]:
            by_ad[r["ad_id"]].append(rec)
    for days in by_ad.values():
        days.sort(key=lambda d: d["date"])
    return by_ad


KEYS = ("spend", "impressions", "clicks", "v3", "thru", "lpv", "atc", "ic", "pur", "value")


def _sum(days: list[dict]) -> dict:
    return {k: sum(d[k] for d in days) for k in KEYS}


def _rates(t: dict, prefix: str) -> dict:
    def div(a, b, pct=False, nd=2):
        return round((100 if pct else 1) * a / b, nd) if b else None
    return {
        f"{prefix}spend": round(t["spend"], 2), f"{prefix}purchases": int(t["pur"]),
        f"{prefix}cpa": div(t["spend"], t["pur"]), f"{prefix}cost_per_ic": div(t["spend"], t["ic"]),
        f"{prefix}cost_per_atc": div(t["spend"], t["atc"]), f"{prefix}link_ctr_pct": div(t["clicks"], t["impressions"], True),
        f"{prefix}hook_rate_pct": div(t["v3"], t["impressions"], True), f"{prefix}hold_rate_pct": div(t["thru"], t["v3"], True),
        f"{prefix}roas": div(t["value"], t["spend"]),
    }


def _cum_at(days: list[dict], threshold: float) -> dict | None:
    """Cumulative totals at the end of the day spend first reached `threshold` (None if it never did)."""
    run = {k: 0.0 for k in KEYS}
    for d in days:
        for k in KEYS:
            run[k] += d[k]
        if run["spend"] >= threshold:
            return dict(run)
    return None


def playbook_checkpoints(days: list[dict], target_cpa: float = TARGET_CPA) -> dict:
    """The playbook's testing rules, applied to the ad's real spend path.

    $12  stop only if nothing at all (no ATC, IC or sale)       — Account Audit #1
    $25  (1x CPA) stop if still no IC and no sale                — Audit #2
    $45  (1.8x CPA) stop if no sale                              — Audit #3 / Checkpoint 2
    ~$55 (2x CPA) graduate with 3+ sales; exactly 2 -> until $75 — Audit #4
    """
    c12, c25, c45 = _cum_at(days, 12), _cum_at(days, target_cpa), _cum_at(days, 1.8 * target_cpa)
    c55, c75 = _cum_at(days, 2.2 * target_cpa), _cum_at(days, 3 * target_cpa)
    out = {
        "at_12_atc_ic_sales": _events(c12), "at_25_ic_sales": _events(c25, ("ic", "pur")),
        "at_45_sales": int(c45["pur"]) if c45 else "", "at_55_sales": int(c55["pur"]) if c55 else "",
        "at_75_sales": int(c75["pur"]) if c75 else "",
    }
    if c12 is None:
        verdict = "too early (<$12 spent)"
    elif c12["atc"] + c12["ic"] + c12["pur"] == 0:
        verdict = "stop at $12: nothing at all"
    elif c25 is None:
        verdict = "in test: passed $12"
    elif c25["ic"] + c25["pur"] == 0:
        verdict = "stop at $25: no checkout, no sale"
    elif c45 is None:
        verdict = "in test: passed $25"
    elif c45["pur"] == 0:
        verdict = "stop at $45: no sale"
    elif c55 is None:
        verdict = "in test: passed $45"
    elif c55["pur"] >= 3:
        verdict = "graduate: 3+ sales by ~$55"
    elif c55["pur"] == 2:
        if c75 is None:
            verdict = "in test: 2 sales, run to $75"
        else:
            verdict = "graduate: 3 sales by $75" if c75["pur"] >= 3 else "stop at $75: under 3 sales"
    else:
        verdict = "stop at ~$55 (or warm retest): under 2 sales"
    out["playbook_verdict"] = verdict
    return out


def _events(c, keys=("atc", "ic", "pur")):
    return "" if c is None else "/".join(str(int(c[k])) for k in keys)


def data_sufficiency(total: dict, target_cpa: float = TARGET_CPA) -> str:
    """Can this ad's results be trusted? Uses the playbook's bar: 3 sales is the first conclusive read."""
    if total["spend"] < target_cpa:
        return "not enough data (<1 CPA spent)"
    if total["pur"] < 3:
        return "not enough data (<3 sales)"
    return "enough data"


def fatigue(days: list[dict], target_cpa: float = TARGET_CPA) -> dict:
    """When did a proven ad start costing too much? First day its trailing-7-day CPA went above 1.5x target
    (with at least one target CPA spent in that window), after it had 3+ sales."""
    out = {"days_to_fatigue": "", "fatigue_date": ""}
    if len(days) < 8:
        return out
    first = date.fromisoformat(days[0]["date"])
    cum_pur = 0.0
    for i, d in enumerate(days):
        cum_pur += d["pur"]
        if cum_pur < 3:
            continue
        day = date.fromisoformat(d["date"])
        window = [x for x in days[: i + 1] if date.fromisoformat(x["date"]) > day - timedelta(days=7)]
        w = _sum(window)
        if w["spend"] >= target_cpa and (w["pur"] == 0 or w["spend"] / w["pur"] > 1.5 * target_cpa):
            out.update(days_to_fatigue=(day - first).days, fatigue_date=d["date"])
            break
    return out


ENDED_LABELS = {
    "too early (<$12 spent)": "stopped before $12",
    "in test: passed $12": "stopped between $12 and $25 (had some activity)",
    "in test: passed $25": "stopped between $25 and $45 (had a checkout or sale)",
    "in test: passed $45": "stopped between $45 and $55 (had a sale)",
    "in test: 2 sales, run to $75": "stopped before $75 with 2 sales",
}


def ad_timeline(days: list[dict], target_cpa: float = TARGET_CPA, data_end: str = "") -> dict:
    if not days:
        return {"first_spend_date": "", "data_sufficiency": "no delivery"}
    spent = [d for d in days if d["spend"] > 0] or days
    first, last = spent[0]["date"], spent[-1]["date"]
    total = _sum(days)
    d0 = date.fromisoformat(first)
    first7 = [d for d in days if date.fromisoformat(d["date"]) <= d0 + timedelta(days=6)]
    first50, run = [], 0.0
    for d in days:
        first50.append(d)
        run += d["spend"]
        if run >= 2 * target_cpa:
            break
    last7_start = date.fromisoformat(last) - timedelta(days=6)
    last7 = [d for d in days if date.fromisoformat(d["date"]) >= last7_start]

    season_spend: dict[str, float] = defaultdict(float)
    for d in days:
        season_spend[season_of(d["date"])] += d["spend"]
    primary_season = max(season_spend, key=season_spend.get) if season_spend else ""
    e7, l7 = _rates(_sum(first7), "first7_"), _rates(_sum(last7), "last7_")

    def change(a, b):
        return round(100 * (b - a) / a, 1) if a and b is not None else ""

    return {
        "first_spend_date": first, "last_spend_date": last,
        "days_with_spend": len(spent), "days_live": (date.fromisoformat(last) - d0).days + 1,
        "months_live": " | ".join(sorted({d["date"][:7] for d in spent})),
        "launch_month": first[:7], "primary_season": primary_season,
        "season_spend_share": " | ".join(f"{k} {round(100 * v / total['spend'])}%" for k, v in
                                         sorted(season_spend.items(), key=lambda kv: -kv[1]) if total["spend"] and v),
        **e7, **_rates(_sum(first50), "first50_"),
        "days_to_spend_50": (date.fromisoformat(first50[-1]["date"]) - d0).days + 1 if run >= 2 * target_cpa else "",
        "last7_link_ctr_pct": l7["last7_link_ctr_pct"], "last7_cpa": l7["last7_cpa"],
        "ctr_change_first7_to_last7_pct": change(e7["first7_link_ctr_pct"], l7["last7_link_ctr_pct"]),
        **fatigue(days, target_cpa), **_ended(playbook_checkpoints(days, target_cpa), last, data_end),
        "data_sufficiency": data_sufficiency(total, target_cpa),
        "_dates": {d["date"] for d in spent},  # for creative-level Motion classification; not written out
    }


def _ended(cp: dict, last_spend: str, data_end: str) -> dict:
    """An ad still "in test" that stopped spending days ago didn't stay in test: say where it was stopped."""
    if data_end and last_spend and last_spend < (date.fromisoformat(data_end) - timedelta(days=2)).isoformat():
        cp["playbook_verdict"] = ENDED_LABELS.get(cp["playbook_verdict"], cp["playbook_verdict"])
    return cp


def monthly_funnels(by_ad: dict[str, list[dict]], ad_info: dict[str, dict]) -> list[dict]:
    """Impressions -> clicks -> landing page views -> ATC -> checkout -> purchase, per product, market,
    funnel stage and month. Shows where each product loses people and how that moves over time."""
    buckets: dict[tuple, dict] = defaultdict(lambda: {k: 0.0 for k in KEYS})
    for ad_id, days in by_ad.items():
        info = ad_info.get(ad_id, {})
        for d in days:
            key = (d["date"][:7], info.get("product", "unknown"), info.get("market", "unknown"),
                   info.get("funnel_stage", "unknown"))
            for k in KEYS:
                buckets[key][k] += d[k]
    rows = []
    for (month, product, market, stage), t in sorted(buckets.items()):
        def pct(a, b):
            return round(100 * a / b, 2) if b else ""

        def cost(a):
            return round(t["spend"] / a, 2) if a else ""
        rows.append({
            "month": month, "product": product, "market": market, "funnel_stage": stage,
            "spend": round(t["spend"], 2), "impressions": int(t["impressions"]), "link_clicks": int(t["clicks"]),
            "landing_page_views": int(t["lpv"]), "add_to_cart": int(t["atc"]), "initiate_checkout": int(t["ic"]),
            "purchases": int(t["pur"]), "purchase_value": round(t["value"], 2),
            "ctr_pct": pct(t["clicks"], t["impressions"]), "click_to_lpv_pct": pct(t["lpv"], t["clicks"]),
            "lpv_to_atc_pct": pct(t["atc"], t["lpv"] or t["clicks"]), "atc_to_ic_pct": pct(t["ic"], t["atc"]),
            "ic_to_purchase_pct": pct(t["pur"], t["ic"]), "cpm": round(1000 * t["spend"] / t["impressions"], 2) if t["impressions"] else "",
            "cost_per_atc": cost(t["atc"]), "cost_per_ic": cost(t["ic"]), "cpa": cost(t["pur"]),
            "roas": round(t["value"] / t["spend"], 2) if t["spend"] else "",
        })
    return rows
