"""Campaign role and audience type for each ad, so creative results are compared like with like.

Retargeting ads reach people who already know the brand and always look cheaper; testing ads run on small
ABO budgets; engagement ads aren't judged on sales. Without these labels the analysis would credit the
creative for what the audience did.
"""
from __future__ import annotations

import re

# Ordered rules on the campaign name (first match wins). Matches the account's naming history:
# "Prospecting Testing", "#17 S&S ABO", "TOF | ABO | Test", "LLA | Sales campaign", "Retargeting - OLD", "BOF | ...".
ROLE_RULES = [
    (r"catalog.*advantage|advantage.*catalog", "catalog_prospecting"),
    (r"catalog", "catalog_retargeting"),
    (r"retention", "retention"),
    (r"retarget|\bbof\b", "retargeting"),
    (r"engagement", "engagement"),
    (r"reach|awareness", "awareness"),
    (r"next[_ ]location", "new_market_test"),
    (r"testing|s&s|\| ?test ?\||split", "testing"),
    (r"\blla\b|lookalike", "prospecting_lookalike"),
    (r"prospecting|\btof\b|scaling|sales", "prospecting"),
]
OBJECTIVE_ROLES = {"OUTCOME_ENGAGEMENT": "engagement", "OUTCOME_AWARENESS": "awareness"}
WARM_ROLES = {"retargeting", "catalog_retargeting", "retention"}


def campaign_role(campaign_name: str, objective: str | None = None) -> str:
    if objective in OBJECTIVE_ROLES:
        return OBJECTIVE_ROLES[objective]
    name = (campaign_name or "").lower()
    for pattern, role in ROLE_RULES:
        if re.search(pattern, name):
            return role
    return "other"


def _names(items) -> list[str]:
    return [str(i.get("name") or i.get("id")) for i in (items or []) if isinstance(i, dict)]


def audience(targeting: dict | None) -> dict:
    """Summarise an ad set's targeting: who was included/excluded and how open the audience was."""
    t = targeting or {}
    included = _names(t.get("custom_audiences"))
    excluded = _names(t.get("excluded_custom_audiences"))
    interests: list[str] = []
    for spec in t.get("flexible_spec") or []:
        for key in ("interests", "behaviors", "life_events", "work_positions", "industries", "education_majors"):
            interests += _names(spec.get(key))
    lookalike = [n for n in included if re.search(r"lookalike|lla|\blal\b|%", n, re.I)]
    warm = [n for n in included if n not in lookalike]
    if warm:
        kind = "warm_custom_audience"
    elif lookalike:
        kind = "lookalike"
    elif interests:
        kind = "interests"
    else:
        kind = "broad"
    geo = t.get("geo_locations") or {}
    gender_codes = t.get("genders") or []
    genders = "all" if not gender_codes or set(gender_codes) == {1, 2} else ("men" if gender_codes == [1] else "women")
    automation = (t.get("targeting_automation") or {}).get("advantage_audience")
    return {
        "audience_type": kind,
        "advantage_audience": bool(automation) if automation is not None else "",
        "age_range": f"{t.get('age_min', '')}-{t.get('age_max', '')}" if t else "",
        "genders": genders if t else "",
        "countries": ",".join(geo.get("countries") or []),
        "interests": " | ".join(interests[:12]),
        "included_audiences": " | ".join(included),
        "excluded_audiences": " | ".join(excluded),
        # Playbook: a "sure TOF" test excludes Web Visitors 30; a "possible MOF" test leaves them in.
        "excludes_web_visitors": any(re.search(r"web ?visitors?|site ?visitors?", n, re.I) for n in excluded),
    }


def funnel_stage(role: str, audience_type: str) -> str:
    if role in ("engagement", "awareness"):
        return role
    if role in WARM_ROLES or audience_type == "warm_custom_audience":
        return "warm (MOF/BOF)"
    return "cold (TOF)"


def market_from_campaign(campaign_name: str, adset_name: str, countries: str) -> str | None:
    text = f"{campaign_name} {adset_name}".lower()
    if re.search(r"_jo\b|jordan", text) or countries == "JO":
        return "JO"
    if countries == "LB":
        return "LB"
    if countries == "SY":
        return "SY"
    return None


def describe(ad: dict, adsets: dict, campaigns: dict) -> dict:
    """All context columns for one ad (insight row fields + ad set/campaign settings)."""
    adset = adsets.get(ad.get("adset_id") or "", {})
    camp = campaigns.get(ad.get("campaign_id") or "", {})
    role = campaign_role(ad.get("campaign_name") or camp.get("name", ""), camp.get("objective"))
    aud = audience(adset.get("targeting"))
    return {
        "campaign_role": role,
        "funnel_stage": funnel_stage(role, aud["audience_type"]),
        "objective": camp.get("objective", ""),
        "budget_type": "CBO" if camp.get("daily_budget") or camp.get("lifetime_budget") else ("ABO" if adset else ""),
        "optimization_goal": adset.get("optimization_goal", ""),
        "bid_strategy": adset.get("bid_strategy", "") or camp.get("bid_strategy", ""),
        **aud,
        "market_from_targeting": market_from_campaign(ad.get("campaign_name", ""), ad.get("adset_name", ""),
                                                      aud["countries"]) or "",
    }
