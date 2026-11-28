"""Settings, read from environment variables (GitHub Actions secrets/vars or a local .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    meta_access_token: str | None = field(default_factory=lambda: _env("META_ACCESS_TOKEN"))
    meta_ad_account_id: str = field(default_factory=lambda: _env("META_AD_ACCOUNT_ID", "1136851577945373"))
    meta_api_version: str = field(default_factory=lambda: _env("META_API_VERSION", "v26.0"))

    gemini_api_key: str | None = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    # "vertex" = Gemini through Google Cloud (covered by the $300 Cloud credit; videos go via a private bucket)
    # "aistudio" = Gemini Developer API key (prepaid AI Studio credits)
    gemini_backend: str = field(default_factory=lambda: _env("GEMINI_BACKEND", "aistudio"))
    gcp_project: str | None = field(default_factory=lambda: _env("GCP_PROJECT"))
    gcp_location: str = field(default_factory=lambda: _env("GCP_LOCATION", "global"))
    gcs_bucket: str | None = field(default_factory=lambda: _env("GCS_BUCKET"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))

    # Google Sheet output is optional: without it, results still land in data/ as JSON + CSV.
    google_service_account_json: str | None = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    sheet_id: str | None = field(default_factory=lambda: _env("SHEET_ID"))

    # Only ads that spent at least this much are watched (metrics are still pulled for all).
    # Hard cap on Gemini spend per calendar month (USD), counted by the app from Gemini's token usage.
    monthly_budget_usd: float = field(default_factory=lambda: float(_env("GEMINI_MONTHLY_BUDGET_USD", "30") or 0))
    min_spend: float = field(default_factory=lambda: float(_env("MIN_SPEND", "0") or 0))
    concurrency: int = field(default_factory=lambda: int(_env("CONCURRENCY", "12")))
    # Stop starting new videos after this many minutes so the job can save before GitHub's 6h limit.
    max_minutes: float = field(default_factory=lambda: float(_env("MAX_MINUTES", "280")))
    max_media: int = field(default_factory=lambda: int(_env("MAX_MEDIA", "0")))  # 0 = no cap
    # Videos longer than this are still analysed, but flagged.
    expected_max_video_seconds: int = 120

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))
    work_dir: Path = field(default_factory=lambda: Path(_env("WORK_DIR", "/tmp/aca-media")))
    manual_dir: Path = field(default_factory=lambda: Path(_env("MANUAL_DIR", "manual")))

    @property
    def graph_base(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}"

    def require_gemini(self) -> None:
        if self.gemini_backend == "vertex":
            self.require("gcp_project", "gcs_bucket")
        else:
            self.require("gemini_api_key")

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit("Missing settings: " + ", ".join(n.upper() for n in missing))
