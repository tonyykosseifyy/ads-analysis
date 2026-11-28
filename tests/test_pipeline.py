"""End-to-end run with a fake Meta API and a fake Gemini, using real creative shapes."""
import csv
import json
from pathlib import Path

import jsonschema
import pytest

import aca.gemini
from aca import main as pipeline
from aca.config import Settings
from aca.meta import MetaError
from aca.schema import ANALYSIS_SCHEMA
from aca.store import Store

FIX = Path(__file__).parent / "fixtures"
C = json.loads((FIX / "creatives.json").read_text())
SAMPLE = json.loads((FIX / "sample_analysis.json").read_text())


def test_schema_is_valid_and_sample_conforms():
    jsonschema.Draft202012Validator.check_schema(ANALYSIS_SCHEMA)
    jsonschema.validate(SAMPLE, ANALYSIS_SCHEMA)


def test_every_object_requires_all_its_fields():
    def walk(node):
        if node.get("type") == "object":
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(ANALYSIS_SCHEMA)


def insight(ad_id, name, spend, purchases):
    return {"ad_id": ad_id, "ad_name": name, "campaign_name": "C", "adset_name": "S", "spend": str(spend),
            "impressions": "10000", "inline_link_clicks": "100",
            "actions": [{"action_type": "omni_purchase", "value": str(purchases)},
                        {"action_type": "video_view", "value": "2500"}]}


class FakeMeta:
    """Ads: two use the same video id, one re-uploads identical bytes under a new id, one is undownloadable."""

    def __init__(self):
        self.ads = {
            "a1": {"creative": C["direct_video"], "effective_status": "ACTIVE"},
            "a2": {"creative": C["direct_video"], "effective_status": "PAUSED"},                # same creative
            "a3": {"creative": {"id": "c3", "video_id": "reupload"}, "effective_status": "PAUSED"},  # same bytes
            "a4": {"creative": C["placement_two_cuts"], "effective_status": "ARCHIVED"},        # download fails
            "a5": {"creative": C["static_link"], "effective_status": "ACTIVE"},
            "a6": {"creative": C["flexible_three_videos"], "effective_status": "ACTIVE"},
        }

    def ad_insights(self):
        return [insight("a1", "Blowout Boost - Hairdresser - Magalie", 300, 12), insight("a2", "copy", 50, 1),
                insight("a3", "reupload", 40, 1), insight("a4", "Whirlwind", 30, 0),
                insight("a5", "Whirlwind - Black Friday - Static", 20, 1), insight("a6", "flex", 90, 3)]

    def ads_by_ids(self, ids):
        return {i: self.ads[i] for i in ids}

    def video_info(self, vid):
        if vid in ("928674400010108", "1015595860890186"):
            raise MetaError("(#10) Application does not have permission")
        return {"source": f"https://cdn/{vid}.mp4", "length": 30}

    def image_urls(self, hashes):
        return {h: f"https://cdn/{h}.jpg" for h in hashes}

    def ig_media(self, mid):
        return {}

    def download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = b"SAME-BYTES" if ("1514336283399725" in url or "reupload" in url) else url.encode()
        dest.write_bytes(content)
        return dest

    def video_asset_breakdown(self, ad_id):
        return [{"video_asset": {"video_id": "a1"}, "spend": "60"}, {"video_asset": {"video_id": "a2"}, "spend": "30"}]


class FakeGemini:
    calls = []
    cost_per_video = 0.0

    def __init__(self, key, model, budget=None, **kw):
        self.model = model
        self.budget = budget

    def analyze(self, path, context=""):
        if self.budget:
            self.budget.check()
            self.budget.record(self.model, {"prompt_token_count": int(FakeGemini.cost_per_video * 1_000_000 / 0.75),
                                            "candidates_token_count": 0})
        FakeGemini.calls.append((self.model, path.name, context))
        out = json.loads(json.dumps(SAMPLE))
        out["one_line_summary"] = f"{self.model}:{path.name}"
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(aca.gemini, "GeminiAnalyzer", FakeGemini)
    FakeGemini.calls = []
    FakeGemini.cost_per_video = 0.0
    s = Settings(meta_access_token="t", gemini_api_key="k", gemini_model="m-flash",
                 google_service_account_json=None, sheet_id=None,
                 data_dir=tmp_path / "data", work_dir=tmp_path / "work", concurrency=3)
    return s


def run(s, meta, **kw):
    store = Store(s.data_dir)
    ads, media = pipeline.collect_ads(meta)
    stats = pipeline.analyze_all(s, meta, store, ads, media, [s.gemini_model], **kw)
    return store, ads, media, stats


def test_full_run_dedupes_and_records_failures(env):
    meta = FakeMeta()
    store, ads, media, stats = run(env, meta)
    # unique assets: direct video, reupload, placement video (fails), static image, 3 rotating videos
    assert set(media) == {"v_1514336283399725", "v_reupload", "v_928674400010108",
                          "i_11310ad57ef7f994eb2c9ed5ac5204d0", "v_a1", "v_a2", "v_a3"}
    assert stats["analysed"] == 5 and stats["duplicates"] == 1 and stats["failed"] == 1
    twins = {"v_reupload", "v_1514336283399725"}
    assert store.canonical("v_reupload") == store.canonical("v_1514336283399725") and store.canonical("v_reupload") in twins
    assert "permission" in store.failures["v_928674400010108"]["reason"]
    assert len(FakeGemini.calls) == 5
    static_ctx = [c[2] for c in FakeGemini.calls if c[1].startswith("i_11310ad")]
    assert static_ctx and "Whirlwind Curl" in static_ctx[0] and "LB" in static_ctx[0]

    # second run: nothing new is watched, failure retried
    store2, _, _, stats2 = run(env, meta)
    assert stats2["queued"] == 1 and stats2["failed"] == 1
    assert store2.failures["v_928674400010108"]["attempts"] == 2


def test_failed_assets_stop_after_three_attempts(env):
    meta = FakeMeta()
    for _ in range(3):
        run(env, meta)
    _, _, _, stats = run(env, meta)
    assert stats["queued"] == 0


def test_retry_failed_watches_only_failed_assets_even_past_the_retry_limit(env):
    meta = FakeMeta()
    for _ in range(3):
        run(env, meta)
    store = Store(env.data_dir)
    _, _, _, stats = run(env, meta, only_keys=set(store.failures))
    assert stats["queued"] == 1  # only the failed placement video, though it already used its 3 attempts
    assert stats["analysed"] == 0 and stats["failed"] == 1


def test_min_spend_and_max_media(env):
    env.min_spend = 45
    _, _, _, stats = run(env, FakeMeta(), dry_run=True)
    assert stats["queued"] == 1  # only the $300+$50 direct video; rotating videos get $30 each
    env.min_spend = 0
    env.max_media = 2
    _, _, _, stats = run(env, FakeMeta(), dry_run=True)
    assert stats["queued"] == 2


def test_outputs_join_metrics_and_analysis(env):
    meta = FakeMeta()
    store, ads, _, _ = run(env, meta)
    pipeline.write_outputs(env, ads, store, {"AssetBreakdown": pipeline.asset_breakdown(meta, ads)}, {"x": 1})
    with open(env.data_dir / "ads.csv", encoding="utf-8-sig") as fh:
        rows = {r["ad_id"]: r for r in csv.DictReader(fh)}
    assert rows["a1"]["market"] == "LB" and rows["a1"]["product"] == "Blowout Boost"
    assert rows["a1"]["hook_type"] == "expert_or_hairdresser"
    assert rows["a3"]["analysed"] == "TRUE"           # via the duplicate alias
    assert rows["a4"]["analysed"] == "FALSE"
    assert "خلّيت" in rows["a1"]["transcript_original"]  # Arabic survives the round trip
    with open(env.data_dir / "creatives.csv", encoding="utf-8-sig") as fh:
        creatives = {r["media_key"]: r for r in csv.DictReader(fh)}
    shared = creatives[store.canonical("v_reupload")]
    assert shared["ads_using"] == "3" and float(shared["spend"]) == 390.0
    assert (env.data_dir / "assetbreakdown.csv").exists()


def test_pilot_runs_each_model_separately(env):
    meta = FakeMeta()
    store = Store(env.data_dir)
    ads, media = pipeline.collect_ads(meta, {"a1", "a5"})
    stats = pipeline.analyze_all(env, meta, store, ads, media, ["gemini-4-argon", "gemini-3.8-flash"], fresh=True)
    pilots = stats["pilot_stores"]
    assert set(pilots) == {"gemini-4-argon", "gemini-3.8-flash"}
    rows = pipeline.pilot_rows(ads, pilots, list(media))
    assert rows[0]["gemini-4-argon | summary"].startswith("gemini-4-argon:")
    assert rows[0]["gemini-3.8-flash | summary"].startswith("gemini-3.8-flash:")
    assert not store.records  # pilot doesn't touch the main results


def test_pilot_one_model_failing_does_not_block_the_other(env, monkeypatch):
    class HalfBroken(FakeGemini):  # noqa: D401
        def analyze(self, path, context=""):
            if self.model == "gemini-4-argon":
                raise RuntimeError("429 model not available on free tier")
            return super().analyze(path, context)
    monkeypatch.setattr(aca.gemini, "GeminiAnalyzer", HalfBroken)
    meta = FakeMeta()
    store = Store(env.data_dir)
    ads, media = pipeline.collect_ads(meta, {"a1"})
    stats = pipeline.analyze_all(env, meta, store, ads, media, ["gemini-4-argon", "gemini-3.8-flash"], fresh=True)
    assert stats["analysed"] == 1 and stats["failed"] == 0
    assert stats["pilot_stores"]["gemini-3.8-flash"].records
    assert not stats["pilot_stores"]["gemini-4-argon"].records


class FakeMetaFull(FakeMeta):
    def __init__(self):
        super().__init__()
        self.ads["a1"].update(adset={"id": "s1"}, campaign={"id": "c1"})

    def ad_insights(self):
        rows = super().ad_insights()
        for r in rows:
            r.update(adset_id="s1" if r["ad_id"] in ("a1", "a2") else "s2",
                     campaign_id="c1" if r["ad_id"] in ("a1", "a2") else "c2",
                     campaign_name="Prospecting Testing" if r["ad_id"] in ("a1", "a2") else "Retargeting")
        return rows

    def adsets(self):
        return {"s1": {"id": "s1", "optimization_goal": "OFFSITE_CONVERSIONS",
                       "targeting": {"geo_locations": {"countries": ["LB"]},
                                     "excluded_custom_audiences": [{"name": "Web Visitors 30"}]}},
                "s2": {"id": "s2", "targeting": {"custom_audiences": [{"name": "ATC 90"}]}}}

    def campaigns(self):
        return {"c1": {"id": "c1", "objective": "OUTCOME_SALES"}, "c2": {"id": "c2", "objective": "OUTCOME_SALES",
                                                                          "daily_budget": "2500"}}

    def daily_insights(self):
        out = []
        for r in self.ad_insights():
            total = float(r["spend"])
            for i in range(10):
                out.append({"ad_id": r["ad_id"], "date_start": f"2026-11-{10 + i:02d}", "spend": str(total / 10),
                            "impressions": "1000", "inline_link_clicks": "10",
                            "actions": [{"action_type": "omni_purchase", "value": "1" if i % 3 == 0 else "0"},
                                        {"action_type": "omni_initiated_checkout", "value": "1"}]})
        return out

    def breakdown_insights(self, dims):
        if dims == ["user_segment_key"]:
            raise MetaError("(#100) breakdown not supported")
        return [{"ad_id": "a1", **{d: "25-34" if d == "age" else "x" for d in dims}, "spend": "50",
                 "impressions": "5000", "actions": [{"action_type": "omni_purchase", "value": "2"}]}]


def test_enrich_builds_every_new_tab(env):
    meta = FakeMetaFull()
    store, ads, media, _ = run(env, meta)
    extra = pipeline.enrich(env, meta, ads, store, copy_analyzer=None)
    extra["AssetBreakdown"] = pipeline.asset_breakdown(meta, ads)
    pipeline.write_outputs(env, ads, store, extra, {"x": 1})
    d = env.data_dir

    def read(name):
        with open(d / f"{name}.csv", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    ads_rows = {r["ad_id"]: r for r in read("ads")}
    a1 = ads_rows["a1"]
    assert a1["campaign_role"] == "testing" and a1["funnel_stage"] == "cold (TOF)" and a1["excludes_web_visitors"] == "TRUE"
    assert ads_rows["a3"]["funnel_stage"] == "warm (MOF/BOF)" and ads_rows["a3"]["budget_type"] == "CBO"
    assert a1["first_spend_date"] == "2026-11-10" and a1["primary_season"] == "regular"
    assert a1["playbook_verdict"] and a1["data_sufficiency"] == "enough data"
    assert a1["motion_class"] in ("winner", "mid-range", "loser", "too new (still running, <28 days)")
    assert a1["ads_manager_link"].endswith("selected_ad_ids=a1")
    assert a1["discount_pct"] == "" and "copy_main_angle" in a1
    cre = {r["media_key"]: r for r in read("creatives")}
    assert cre[store.canonical("v_reupload")]["days_with_spend"] == "10"
    assert {r["dimension"] for r in read("breakdowns")} >= {"age", "gender", "age+gender", "region"}
    assert "user_segment_key" not in {r["dimension"] for r in read("breakdowns")}  # unsupported -> skipped, run continues
    funnels = read("funnels")
    assert funnels and {r["funnel_stage"] for r in funnels} == {"cold (TOF)", "warm (MOF/BOF)"}
    assert (d / "daily.csv").exists() and (d / "hooktests.csv").exists()


def test_monthly_cap_stops_new_videos_and_resumes_after_raise(env):
    from datetime import date
    FakeGemini.cost_per_video = 0.40           # each analysed video costs $0.40 (priced as gemini-3.8-flash input)
    env.gemini_model = "gemini-3.8-flash"
    env.monthly_budget_usd = 1.0               # room for ~2-3 videos
    env.concurrency = 1                        # deterministic order
    store, ads, media, stats = run(env, FakeMeta())
    assert stats["analysed"] + stats["duplicates"] >= 2 and stats["stopped_for_budget"] >= 2
    assert stats["gemini_usd_this_month"] >= 1.0 and stats["gemini_usd_this_month"] < 1.0 + 0.41
    spend = json.loads((env.data_dir / "spend.json").read_text())
    assert spend[date.today().isoformat()[:7]]["usd"] == round(stats["gemini_usd_this_month"], 4)
    # nothing new watched until the cap is raised; the next run resumes where it stopped
    _, _, _, again = run(env, FakeMeta())
    assert again["analysed"] == 0 and again["stopped_for_budget"] >= 2
    env.monthly_budget_usd = 10.0
    _, _, _, resumed = run(env, FakeMeta())
    assert resumed["analysed"] >= 2 and resumed["stopped_for_budget"] == 0
