"""Meta Marketing API: ads that ran, their metrics, their creatives, and the media files behind them."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from .config import Settings

log = logging.getLogger(__name__)

INSIGHT_FIELDS = [
    "ad_id", "ad_name", "adset_id", "adset_name", "campaign_id", "campaign_name",
    "spend", "impressions", "reach", "frequency", "cpm", "clicks", "ctr",
    "inline_link_clicks", "inline_link_click_ctr",
    "actions", "action_values",
    "video_thruplay_watched_actions", "video_p25_watched_actions", "video_p50_watched_actions",
    "video_p75_watched_actions", "video_p100_watched_actions", "video_avg_time_watched_actions",
    "date_start", "date_stop",
]

DAILY_FIELDS = [
    "ad_id", "spend", "impressions", "inline_link_clicks", "actions", "action_values",
    "video_thruplay_watched_actions", "date_start",
]
BREAKDOWN_FIELDS = ["ad_id", "spend", "impressions", "reach", "inline_link_clicks", "actions", "action_values"]
ADSET_FIELDS = ("id,name,campaign_id,effective_status,optimization_goal,billing_event,bid_strategy,daily_budget,"
                "lifetime_budget,start_time,end_time,targeting")
CAMPAIGN_FIELDS = "id,name,objective,effective_status,buying_type,smart_promotion_type,daily_budget,lifetime_budget"

CREATIVE_FIELDS = (
    "id,name,object_type,video_id,image_url,image_hash,thumbnail_url,body,title,"
    "object_story_spec,asset_feed_spec,effective_object_story_id,"
    "effective_instagram_media_id,source_instagram_media_id"
)
AD_FIELDS = f"id,name,effective_status,created_time,adset{{id,name}},campaign{{id,name,objective}},creative{{{CREATIVE_FIELDS}}}"


class MetaError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        settings.require("meta_access_token")
        self.s = settings
        self.http = session or requests.Session()

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path_or_url: str, params: dict | None = None, retries: int = 5) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{self.s.graph_base}/{path_or_url.lstrip('/')}"
        params = dict(params or {})
        if "access_token=" not in url:
            params["access_token"] = self.s.meta_access_token
        for attempt in range(retries):
            resp = self.http.request(method, url, params=params, timeout=120)
            if resp.status_code == 200:
                return resp.json()
            body = _safe_json(resp)
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = err.get("code")
            # 4/17/32/613/8000x = rate limits; 1/2 = transient. Anything else (bad parameter, permission,
            # expired token) will fail the same way again, so it is raised straight away.
            transient = code in (1, 2, 4, 17, 32, 613, 80000, 80004) or (resp.status_code >= 500 and code in (None, 1, 2))
            if transient:
                wait = min(300, 15 * (2 ** attempt))
                log.warning("Meta API busy (%s %s), retrying in %ss", code, err.get("message"), wait)
                time.sleep(wait)
                continue
            raise MetaError(f"{resp.status_code} {err.get('message') or resp.text[:300]}")
        raise MetaError(f"Gave up after {retries} retries: {url}")

    def get(self, path: str, **params: Any) -> dict:
        return self._request("GET", path, params)

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        data = self.get(path, **params)
        while True:
            yield from data.get("data", [])
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                return
            data = self._request("GET", nxt)

    # ---- ads that ran + metrics -----------------------------------------
    def _async_insights(self, params: dict, label: str) -> list[dict]:
        """Run an insights report as an async job (big lifetime / daily / breakdown reports time out otherwise)."""
        act = f"act_{self.s.meta_ad_account_id}"
        job = self._request("POST", f"{act}/insights", {"use_account_attribution_setting": "true", "limit": 500, **params})
        run_id = job["report_run_id"]
        for _ in range(360):  # up to ~30 min
            status = self.get(run_id, fields="async_status,async_percent_completion")
            if status.get("async_status") == "Job Completed":
                break
            if status.get("async_status") in ("Job Failed", "Job Skipped"):
                raise MetaError(f"{label} report failed: {status}")
            time.sleep(5)
        else:
            raise MetaError(f"{label} report did not finish in time")
        rows = list(self.paginate(f"{run_id}/insights", limit=500))
        log.info("%s: %s rows", label, len(rows))
        return rows

    def ad_insights(self) -> list[dict]:
        """Lifetime metrics for every ad that delivered, including paused/archived/deleted ones."""
        return self._async_insights({"level": "ad", "date_preset": "maximum", "fields": ",".join(INSIGHT_FIELDS)},
                                    "Lifetime ad metrics")

    def daily_insights(self) -> list[dict]:
        """One row per ad per day with delivery: the basis for run dates, early windows, fatigue and funnels."""
        return self._async_insights({"level": "ad", "date_preset": "maximum", "time_increment": 1,
                                     "fields": ",".join(DAILY_FIELDS)}, "Daily ad metrics")

    def breakdown_insights(self, breakdowns: list[str]) -> list[dict]:
        """Lifetime metrics per ad split by audience/placement dimensions (age, gender, placement, region...)."""
        return self._async_insights({"level": "ad", "date_preset": "maximum", "breakdowns": ",".join(breakdowns),
                                     "fields": ",".join(BREAKDOWN_FIELDS)}, "Breakdown " + "+".join(breakdowns))

    def adsets(self) -> dict[str, dict]:
        """Every ad set (all statuses) with its targeting and optimisation settings."""
        out = {}
        for a in self.paginate(f"act_{self.s.meta_ad_account_id}/adsets", fields=ADSET_FIELDS, limit=100,
                               filtering=json.dumps([{"field": "effective_status", "operator": "IN",
                                                      "value": self.ALL_STATUSES}])):
            out[a["id"]] = a
        return out

    def campaigns(self) -> dict[str, dict]:
        out = {}
        for c in self.paginate(f"act_{self.s.meta_ad_account_id}/campaigns", fields=CAMPAIGN_FIELDS, limit=100,
                               filtering=json.dumps([{"field": "effective_status", "operator": "IN",
                                                      "value": self.ALL_STATUSES}])):
            out[c["id"]] = c
        return out

    ALL_STATUSES = ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
                    "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS", "WITH_ISSUES"]

    def ads_by_ids(self, ad_ids: Iterable[str]) -> dict[str, dict]:
        """Creative + names for the given ads.

        Lists the account's ads (every status, incl. archived/deleted) page by page, then looks up
        any ad still missing one by one. (The batch `?ids=` lookup was removed by Meta in v26.)
        """
        wanted = set(ad_ids)
        out: dict[str, dict] = {}
        try:
            for ad in self.paginate(f"act_{self.s.meta_ad_account_id}/ads", fields=AD_FIELDS, limit=50,
                                    filtering=json.dumps([{"field": "effective_status", "operator": "IN",
                                                           "value": self.ALL_STATUSES}])):
                if ad.get("id") in wanted:
                    out[ad["id"]] = ad
        except MetaError as e:
            log.warning("Listing ads failed part-way (%s); looking up the rest one by one", e)
        missing = [i for i in wanted if i not in out]
        if missing:
            log.info("%s ads not in the account listing; fetching individually", len(missing))
        for ad_id in missing:
            try:
                out[ad_id] = self.get(ad_id, fields=AD_FIELDS)
            except MetaError as e2:
                log.warning("Ad %s unavailable: %s", ad_id, e2)
        return out

    def video_asset_breakdown(self, ad_id: str) -> list[dict]:
        """Per-video metrics for ads that rotate several different videos."""
        try:
            return list(self.paginate(
                f"{ad_id}/insights", date_preset="maximum", breakdowns="video_asset",
                fields="spend,impressions,inline_link_clicks,actions,video_thruplay_watched_actions",
                use_account_attribution_setting="true",
            ))
        except MetaError as e:
            log.info("No video_asset breakdown for %s: %s", ad_id, e)
            return []

    # ---- media -----------------------------------------------------------
    def video_info(self, video_id: str) -> dict:
        return self.get(video_id, fields="id,source,length,permalink_url,title")

    def image_urls(self, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        rows = self.get(f"act_{self.s.meta_ad_account_id}/adimages",
                        hashes=json.dumps(hashes), fields="hash,url,permalink_url")
        return {r["hash"]: (r.get("url") or r.get("permalink_url")) for r in rows.get("data", [])}

    def ig_media(self, media_id: str) -> dict:
        return self.get(media_id, fields="id,media_type,media_url,thumbnail_url,permalink")

    def download(self, url: str, dest: Path, max_bytes: int = 500 * 1024 * 1024) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            size = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MetaError(f"File larger than {max_bytes // 1024 // 1024} MB: {url[:80]}")
                    fh.write(chunk)
        return dest


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}
