"""Hard spending cap for Gemini, enforced by the app itself.

Google Cloud has no automatic stop, so every Gemini response's token counts are priced here and added to a
monthly total kept in data/spend.json. Once the month's total reaches the cap, no new video or copy call is
started (calls already in flight finish, so the overshoot is at most a few cents per parallel worker).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output incl. thinking). Flash introductory prices run to 31 Dec 2026.
PRICES = {
    "gemini-3.8-flash": [("2026-12-31", 0.75, 3.75), ("9999-12-31", 1.50, 7.50)],
    "gemini-3.7-flash": [("2026-12-31", 0.75, 3.75), ("9999-12-31", 1.50, 7.50)],
    "gemini-3.5-flash-lite": [("9999-12-31", 0.30, 2.50)],
    "gemini-4-argon": [("2026-11-30", 2.00, 10.00), ("9999-12-31", 4.00, 20.00)],
}
UNKNOWN_MODEL_PRICE = (4.00, 20.00)  # unknown model: assume flagship prices so the cap errs on the safe side


def price_for(model: str, today: date | None = None) -> tuple[float, float]:
    day = (today or date.today()).isoformat()
    for until, p_in, p_out in PRICES.get(model, []):
        if day <= until:
            return p_in, p_out
    return UNKNOWN_MODEL_PRICE


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, path: Path, monthly_cap_usd: float):
        self.path = path
        self.cap = monthly_cap_usd
        self.month = date.today().isoformat()[:7]
        self._lock = threading.Lock()
        data = json.loads(path.read_text()) if path.exists() else {}
        self.data = data
        self.spent_before_run = float(data.get(self.month, {}).get("usd", 0.0))
        self.run_usd = 0.0
        self.run_tokens = {"input": 0, "output": 0}

    @property
    def month_usd(self) -> float:
        return self.spent_before_run + self.run_usd

    def check(self) -> None:
        if self.cap and self.month_usd >= self.cap:
            raise BudgetExceeded(f"monthly Gemini cap reached (${self.month_usd:.2f} of ${self.cap:.2f})")

    def remaining(self) -> float:
        return max(0.0, self.cap - self.month_usd) if self.cap else float("inf")

    def record(self, model: str, usage) -> float:
        """Price one response's usage metadata and add it to the month's total. Returns the call's cost."""
        p_in, p_out = price_for(model)
        inp = _get(usage, "prompt_token_count")
        out = _get(usage, "candidates_token_count") + _get(usage, "thoughts_token_count")
        cost = (inp * p_in + out * p_out) / 1_000_000
        with self._lock:
            self.run_usd += cost
            self.run_tokens["input"] += inp
            self.run_tokens["output"] += out
            month = self.data.setdefault(self.month, {"usd": 0.0, "input_tokens": 0, "output_tokens": 0})
            month["usd"] = round(self.spent_before_run + self.run_usd, 4)
            month["input_tokens"] = month.get("input_tokens", 0) + inp
            month["output_tokens"] = month.get("output_tokens", 0) + out
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1))
        return cost

    def summary(self) -> dict:
        return {"gemini_usd_this_run": round(self.run_usd, 3), "gemini_usd_this_month": round(self.month_usd, 3),
                "monthly_cap_usd": self.cap, **{f"{k}_tokens_this_run": v for k, v in self.run_tokens.items()}}


def _get(usage, name: str) -> int:
    if usage is None:
        return 0
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(v or 0)
