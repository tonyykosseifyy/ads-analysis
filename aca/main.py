"""Run the pipeline.

    python -m aca.main                       # normal run: refresh metrics, watch every new ad asset
    python -m aca.main --dry-run             # list what would be watched, no Gemini calls
    python -m aca.main --metrics-only        # refresh numbers in the Sheet only
    python -m aca.main --pilot --ad-ids 1,2 --models gemini-4-argon,gemini-3.8-flash
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .config import Settings
from .meta import MetaClient, MetaError
from .parse import MediaRef, ad_copy, market_for, metrics_from_insight, product_for, resolve_media
from .store import Store, now_iso, slug
PROCESS_START = time.time()  # the time cap counts from here, so collecting ads counts too

from .tables import ad_rows, creative_columns, creative_rows, to_grid, write_csv

log = logging.getLogger("aca")


# ---------------------------------------------------------------------------
# 1. Ads + metrics
# ---------------------------------------------------------------------------
def collect_ads(meta: MetaClient, only_ids: set[str] | None = None) -> tuple[list[dict], dict[str, MediaRef]]:
    insights = meta.ad_insights()
    if only_ids:
        insights = [r for r in insights if r["ad_id"] in only_ids]
    log.info("%s ads delivered in the account's lifetime", len(insights))
    details = meta.ads_by_ids(r["ad_id"] for r in insights)

    ads, media = [], {}
    for row in insights:
        d = details.get(row["ad_id"], {})
        creative = d.get("creative") or {}
        refs = resolve_media(creative)
        copy = ad_copy(creative)
        for ref in refs:
            media.setdefault(ref.key, ref)
        ads.append({
            "ad_id": row["ad_id"], "ad_name": row.get("ad_name", ""),
            "campaign_id": row.get("campaign_id", ""), "adset_id": row.get("adset_id", ""),
            "campaign_name": row.get("campaign_name", ""), "adset_name": row.get("adset_name", ""),
            "creative": {k: creative.get(k) for k in ("id", "effective_object_story_id", "effective_instagram_media_id")},
            "status": d.get("effective_status", "unavailable"), "created_time": d.get("created_time", ""),
            "creative_id": creative.get("id", ""),
            "market": market_for(copy["landing_urls"]), "product": product_for(copy["landing_urls"], row.get("ad_name", "")),
            "copy": copy, "metrics": metrics_from_insight(row),
            "media_keys": [r.key for r in refs], "rotating": any(r.role == "rotating" for r in refs),
        })
    ads.sort(key=lambda a: -a["metrics"]["spend"])
    return ads, media


# ---------------------------------------------------------------------------
# 2. Download one asset
# ---------------------------------------------------------------------------
def download_media(meta: MetaClient, ref: MediaRef, work: Path) -> tuple[Path, dict]:
    info: dict = {"kind": ref.kind, "id": ref.id}
    if ref.kind == "image":
        url = ref.image_url or meta.image_urls([ref.id]).get(ref.id)
        if not url:
            raise MetaError("image URL not available")
        path = meta.download(url, work / f"{slug(ref.key)}.jpg")
        return path, info

    if ref.kind == "video":
        for vid in [ref.id, *ref.alt_ids]:
            try:
                v = meta.video_info(vid)
            except MetaError as e:
                info.setdefault("errors", []).append(f"{vid}: {e}")
                v = {}
            if not v.get("source") and hasattr(meta, "video_info_as_page"):
                # Page videos: Meta gives the file link only to the Page's own token
                v = meta.video_info_as_page(vid) or v
                if not v.get("source") and not any(e.startswith(f"{vid}:") for e in info.get("errors", [])):
                    info.setdefault("errors", []).append(f"{vid}: no file link (not owned by our Pages or ad account)")
            if v.get("source"):
                info.update(video_id=vid, length_s=v.get("length"), permalink=v.get("permalink_url"))
                dest = work / f"{slug(ref.key)}.mp4"
                try:
                    return meta.download(v["source"], dest), info
                except requests.HTTPError:
                    # the signed file link may have expired since it was listed: ask for a new one
                    v = meta.video_info(vid, fresh=True)
                    if v.get("source"):
                        return meta.download(v["source"], dest), info
                    raise
        if not ref.ig_fallback:
            raise MetaError("video file not available to this token: " + "; ".join(info.get("errors", [])))
        ig_id = ref.ig_fallback
    else:
        ig_id = ref.id

    earlier = "; ".join(info.get("errors", []))
    try:
        m = meta.ig_media(ig_id)
    except MetaError as e:
        raise MetaError(f"Instagram fallback {ig_id} failed ({e}); video errors: {earlier}") from e
    if not m.get("media_url"):
        raise MetaError(f"Instagram media {ig_id} has no downloadable file; video errors: {earlier}")
    ext = ".mp4" if m.get("media_type") in ("VIDEO", "REELS") else ".jpg"
    info.update(ig_media_id=ig_id, permalink=m.get("permalink"))
    return meta.download(m["media_url"], work / f"{slug(ref.key)}{ext}"), info


MEDIA_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp"}


def manual_files(folder: Path, ads: list[dict], media: dict) -> dict[str, Path]:
    """Video/image files added by hand to manual/, for creatives Meta won't hand over (creators' own posts,
    Instagram-only posts). Name each file after its ad id (e.g. 23857964199420795.mp4) or its media key
    (e.g. v_3676247799180361.mp4); it is then analysed and joined to that ad's metrics like any other."""
    if not folder.is_dir():
        return {}
    by_ad = {a["ad_id"]: a["media_keys"] for a in ads if a.get("media_keys")}
    out: dict[str, Path] = {}
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in MEDIA_EXTS:
            continue
        if f.stem in media:
            out[f.stem] = f
        elif f.stem in by_ad:
            for k in by_ad[f.stem]:
                out.setdefault(k, f)
        else:
            log.warning("manual/%s matches no ad id or media key; skipped", f.name)
    if out:
        log.info("%s creatives have a manual file", len(out))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _context(ref_key: str, ads: list[dict]) -> str:
    users = [a for a in ads if ref_key in a["media_keys"]][:3]
    products = sorted({a["product"] for a in users})
    markets = sorted({a["market"] for a in users})
    return f"Product(s) this ad links to: {', '.join(products)}. Market(s): {', '.join(markets)}."


# ---------------------------------------------------------------------------
# 3. Watch assets with Gemini
# ---------------------------------------------------------------------------
def _budget(settings: Settings):
    from .budget import Budget
    return Budget(settings.data_dir / "spend.json", settings.monthly_budget_usd)


def analyze_all(settings: Settings, meta: MetaClient, store: Store, ads: list[dict], media: dict[str, MediaRef],
                models: list[str], fresh: bool = False, dry_run: bool = False, budget=None,
                only_keys: set[str] | None = None) -> dict:
    """only_keys: watch just these assets (e.g. the ones that failed before), even past their retry limit."""
    spend_by_key: dict[str, float] = {}
    for ad in ads:
        for k in ad["media_keys"]:
            spend_by_key[k] = spend_by_key.get(k, 0) + ad["metrics"]["spend"] / max(len(ad["media_keys"]), 1)

    manual = manual_files(settings.manual_dir, ads, media)
    todo = [k for k in sorted(media, key=lambda k: -spend_by_key.get(k, 0))
            if spend_by_key.get(k, 0) >= settings.min_spend
            and (fresh or not store.is_done(k))
            and (fresh or only_keys is not None or k in manual or not store.should_skip_failed(k))
            and (only_keys is None or k in only_keys)]
    if settings.max_media:
        todo = todo[: settings.max_media]
    stats = {"queued": len(todo), "analysed": 0, "duplicates": 0, "failed": 0, "stopped_for_time": 0}
    log.info("%s assets to watch (%s already done)", len(todo), sum(store.is_done(k) for k in media))
    if dry_run:
        for k in todo:
            log.info("would watch %s  ($%.2f)", k, spend_by_key.get(k, 0))
        return stats

    from . import gemini
    from .budget import BudgetExceeded
    from .gemini import ModelUnavailable
    unavailable: set[str] = set()
    budget = budget or _budget(settings)
    analyzers = {m: gemini.make_analyzer(settings, m, budget) for m in models}
    stats["stopped_for_budget"] = 0
    started = PROCESS_START
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    if todo and hasattr(meta, "prefetch_media"):
        meta.prefetch_media()

    def work(key: str) -> str:
        if (time.time() - started) / 60 > settings.max_minutes:
            return "stopped_for_time"
        if budget.remaining() <= 0:
            return "stopped_for_budget"
        ref = media[key]
        tmp = settings.work_dir / slug(key)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            if key in manual:
                path, info = manual[key], {"kind": ref.kind, "id": ref.id, "source": "manual file"}
            else:
                path, info = download_media(meta, ref, tmp)
            digest = _sha256(path)
            if not fresh:
                owner = store.claim(digest, key)
                if owner:
                    store.alias(key, owner)
                    return "duplicates"
            errors = []
            for model, analyzer in analyzers.items():
                if model in unavailable:
                    errors.append(f"{model}: not available on this API key's plan")
                    continue
                try:  # each model on its own, so one model being unavailable doesn't block the others
                    result = analyzer.analyze(path, context=_context(key, ads))
                except BudgetExceeded:
                    raise
                except ModelUnavailable as e:
                    log.warning("%s — skipping it for the rest of this run", e)
                    unavailable.add(model)
                    errors.append(f"{model}: {e}")
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("%s failed on %s: %s", model, key, e)
                    errors.append(f"{model}: {e}")
                    continue
                flags = []
                if (result.get("duration_s") or 0) > settings.expected_max_video_seconds:
                    flags.append("longer_than_2_min")
                store_for(model).save(key, {"model": model, "sha256": digest, "source": info,
                                            "role": ref.role, "flags": flags, "analysis": result})
            if len(errors) == len(analyzers):
                raise RuntimeError(" | ".join(errors))
            return "analysed"
        except BudgetExceeded as e:
            log.warning("Not watching %s: %s", key, e)
            return "stopped_for_budget"
        except Exception as e:  # noqa: BLE001 - one bad asset must not stop the run
            log.warning("FAILED %s: %s", key, str(e)[:300])
            if not fresh:  # pilot failures must not count against the real run's retry budget
                store.fail(key, str(e))
            return "failed"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # Created up front (not lazily inside threads) so parallel workers share one store per model.
    pilot_stores: dict[str, Store] = (
        {m: Store(settings.data_dir / "pilot", subdir=slug(m)) for m in models} if fresh else {})

    def store_for(model: str) -> Store:
        return pilot_stores[model] if fresh else store

    with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
        futures = {pool.submit(work, k): k for k in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            outcome = fut.result()
            stats[outcome] += 1
            if i % 10 == 0 or i == len(todo):
                log.info("progress %s/%s %s", i, len(todo), stats)
    stats["pilot_stores"] = pilot_stores
    stats.update(budget.summary())
    if stats["stopped_for_budget"]:
        log.warning("Monthly Gemini cap of $%.2f reached: %s assets left for later (raise the cap or wait for next month)",
                    settings.monthly_budget_usd, stats["stopped_for_budget"])
    return stats


# ---------------------------------------------------------------------------
# 4. Outputs
# ---------------------------------------------------------------------------
def asset_breakdown(meta: MetaClient, ads: list[dict], limit: int = 60) -> list[dict]:
    rows = []
    for ad in [a for a in ads if a["rotating"]][:limit]:
        for r in meta.video_asset_breakdown(ad["ad_id"]):
            va = r.get("video_asset") or {}
            m = metrics_from_insight(r)
            rows.append({"ad_id": ad["ad_id"], "ad_name": ad["ad_name"], "video_id": va.get("video_id", ""),
                         "media_key": f"v_{va.get('video_id')}" if va.get("video_id") else "",
                         "spend": m["spend"], "impressions": m["impressions"], "link_clicks": m["link_clicks"],
                         "purchases": m["purchases"], "initiate_checkout": m["initiate_checkout"],
                         "thruplays": m["thruplays"]})
    return rows


def pilot_rows(ads, pilot_stores: dict[str, Store], keys: list[str]) -> list[dict]:
    rows = []
    for key in keys:
        row = {"media_key": key, "ad_names": " | ".join(a["ad_name"] for a in ads if key in a["media_keys"])[:300]}
        for model, st in pilot_stores.items():
            c = creative_columns(st.get(key))
            for col in ("summary", "primary_language", "dialect", "transcript_original", "transcript_english",
                        "on_screen_text", "hook_type", "format", "main_angle"):
                row[f"{model} | {col}"] = c.get(col, "")
        rows.append(row)
    return rows


BREAKDOWNS = [["age"], ["gender"], ["age", "gender"], ["publisher_platform", "platform_position"], ["region"],
              ["user_segment_key"]]  # user_segment_key = new / engaged / existing customers (Advantage+ campaigns)


def enrich(settings: Settings, meta: MetaClient, ads: list[dict], store: Store, copy_analyzer=None) -> dict:
    """Everything beyond the lifetime totals: campaign/audience context, daily timeline, playbook checkpoints,
    breakdowns, monthly funnels, ad-copy tags, creator and links. Only Meta data (free) plus cheap copy tags."""
    from .classify import creator_from_name, links
    from .context import describe
    from .copy_analysis import analyze_copies
    from .timeline import ad_timeline, group_daily, monthly_funnels

    extra: dict = {}
    try:
        adsets, campaigns = meta.adsets(), meta.campaigns()
    except MetaError as e:
        log.warning("Ad set / campaign settings unavailable: %s", e)
        adsets, campaigns = {}, {}
    for ad in ads:
        ad["context"] = describe(ad, adsets, campaigns)
        if ad["market"] == "unknown" and ad["context"]["market_from_targeting"]:
            ad["market"] = ad["context"]["market_from_targeting"]
        ad["creator"] = creator_from_name(ad["ad_name"])
        first = ad["media_keys"][0] if ad["media_keys"] else None
        rec = store.get(first) if first else None
        if ad["product"] in ("unknown", "multi-product") and rec:  # no product page: use what the video shows
            from .parse import product_for
            shown = product_for([], "", (rec.get("analysis") or {}).get("product", {}).get("products_shown"))
            if shown not in ("unknown", "multi-product"):
                ad["product"] = shown
        ad["links"] = links(ad["ad_id"], settings.meta_ad_account_id, ad.get("creative") or {}, (rec or {}).get("source"))

    daily = meta.daily_insights()
    by_ad = group_daily(daily)
    extra["data_end_date"] = max((r.get("date_start") or "" for r in daily), default="")
    for ad in ads:
        ad["timeline"] = ad_timeline(by_ad.get(ad["ad_id"], []), data_end=extra["data_end_date"])
    info = {a["ad_id"]: {"product": a["product"], "market": a["market"], "funnel_stage": a["context"]["funnel_stage"]}
            for a in ads}
    extra["Funnels"] = monthly_funnels(by_ad, info)
    _write_daily(settings.data_dir / "daily.csv", daily)

    raw = []
    for dims in BREAKDOWNS:
        try:
            for r in meta.breakdown_insights(dims):
                r["_dimension"] = "+".join(dims)
                r["_value"] = " / ".join(str(r.get(d, "")) for d in dims)
                raw.append(r)
        except MetaError as e:
            log.warning("Breakdown %s unavailable: %s", "+".join(dims), str(e)[:200])
    extra["_breakdowns_raw"] = raw

    tags = analyze_copies(ads, copy_analyzer, settings.data_dir / "copy", settings.concurrency)
    for ad in ads:
        ad["copy_tags"] = tags
    return extra


def _write_daily(path: Path, daily: list[dict]) -> None:
    from .timeline import day_record
    rows = [{"ad_id": r["ad_id"], **day_record(r)} for r in daily]
    write_csv(rows, path)


def write_outputs(settings: Settings, ads: list[dict], store: Store, extra_tabs: dict, run_log: dict) -> None:
    from .tables import breakdown_rows, hook_test_rows
    d = settings.data_dir
    extra_tabs = dict(extra_tabs)
    data_end = extra_tabs.pop("data_end_date", "")
    raw_breakdowns = extra_tabs.pop("_breakdowns_raw", [])
    creatives = creative_rows(ads, store, data_end)
    tables = {"Ads": ad_rows(ads, store, creatives), "Creatives": creatives, "HookTests": hook_test_rows(creatives)}
    if raw_breakdowns:
        tables["Breakdowns"] = breakdown_rows(raw_breakdowns, ads, creatives, store)
    tables.update(extra_tabs)
    slim = [{k: v for k, v in a.items() if k not in ("timeline", "copy_tags")} for a in ads]
    (d / "ads.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for name, rows in tables.items():
        write_csv(rows, d / f"{name.lower()}.csv")

    runs_path = d / "runs.json"
    runs = json.loads(runs_path.read_text()) if runs_path.exists() else []
    runs.append(run_log)
    runs_path.write_text(json.dumps(runs[-200:], indent=1), encoding="utf-8")
    tables["Runs"] = list(reversed(runs[-200:]))
    tables["Failures"] = [{"media_key": k, **v} for k, v in sorted(store.failures.items())]

    if settings.google_service_account_json and settings.sheet_id:
        from .sheets import write_tabs
        write_tabs(settings.google_service_account_json, settings.sheet_id,
                   {name: to_grid(rows) for name, rows in tables.items()})
    else:
        log.info("No Google Sheet configured; results are in %s/*.csv", d)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Study every ad in the Meta ad account.")
    p.add_argument("--ad-ids", default="", help="comma-separated ad ids to limit the run to")
    p.add_argument("--models", default="", help="comma-separated Gemini models (default: GEMINI_MODEL)")
    p.add_argument("--pilot", action="store_true", help="re-watch the given ads with each model into data/pilot/")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--metrics-only", action="store_true")
    p.add_argument("--retry-failed", action="store_true",
                   help="watch only the assets that failed before (e.g. after fixing token permissions)")
    p.add_argument("--max-media", type=int, default=None)
    p.add_argument("--min-spend", type=float, default=None)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = Settings()
    if args.max_media is not None:
        s.max_media = args.max_media
    if args.min_spend is not None:
        s.min_spend = args.min_spend
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [s.gemini_model]
    only = {i.strip() for i in args.ad_ids.split(",") if i.strip()} or None
    if args.pilot and not only:
        raise SystemExit("--pilot needs --ad-ids")

    run = {"started_at": now_iso(), "mode": "pilot" if args.pilot else ("metrics" if args.metrics_only else
                                                        ("retry-failed" if args.retry_failed else "run")),
           "models": ",".join(models)}
    meta = MetaClient(s)
    store = Store(s.data_dir)
    ads, media = collect_ads(meta, only)
    run.update(ads=len(ads), assets=len(media))

    extra: dict[str, list[dict]] = {}
    if args.pilot:
        stats = analyze_all(s, meta, store, ads, media, models, fresh=True, dry_run=args.dry_run)
        pilot_stores = stats.pop("pilot_stores", {})
        rows = pilot_rows(ads, pilot_stores, list(media))
        write_csv(rows, s.data_dir / "pilot" / "pilot.csv")
        extra["Pilot"] = rows
        run.update(stats)
        # Pilot only touches the Pilot tab; the main tables stay as they are.
        if s.google_service_account_json and s.sheet_id:
            from .sheets import write_tabs
            write_tabs(s.google_service_account_json, s.sheet_id, {"Pilot": to_grid(rows)})
        run["finished_at"] = now_iso()
        log.info("Pilot done: %s", run)
        return

    if not args.metrics_only:
        retry = set(store.failures) if args.retry_failed else None
        if retry is not None:
            log.info("Retrying only the %s assets that failed before", len(retry))
        stats = analyze_all(s, meta, store, ads, media, models, dry_run=args.dry_run, only_keys=retry)
        stats.pop("pilot_stores", None)
        run.update(stats)
    if only:
        log.info("Limited run (--ad-ids): analyses saved, tables not rewritten. Done: %s", run)
        return
    if not args.dry_run:
        copy_analyzer = None
        if (s.gemini_api_key or s.gemini_backend == "vertex") and not args.metrics_only:
            from .gemini import make_analyzer
            copy_analyzer = make_analyzer(s, models[0], _budget(s))
        extra.update(enrich(s, meta, ads, store, copy_analyzer))
        extra["AssetBreakdown"] = asset_breakdown(meta, ads)
        run["finished_at"] = now_iso()
        write_outputs(s, ads, store, extra, run)
    log.info("Done: %s", run)


if __name__ == "__main__":
    main()
