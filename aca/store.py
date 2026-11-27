"""Results kept in the repo's data/ folder, so every run knows what has already been watched."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_ATTEMPTS = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")


class Store:
    """data/analyses/<media_key>.json   one file per watched video/image (the full Gemini output)
    data/aliases.json                  media keys whose file is byte-identical to one already watched
    data/failures.json                 media that could not be downloaded/analysed yet (retried up to 3x)
    """

    def __init__(self, root: Path, subdir: str = "analyses"):
        self.root = root
        self.dir = root / subdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {}
        for p in sorted(self.dir.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            self.records[rec["media_key"]] = rec
        self.by_sha = {r["sha256"]: k for k, r in self.records.items() if r.get("sha256")}
        self.aliases: dict[str, str] = self._load("aliases.json")
        self.failures: dict[str, dict] = self._load("failures.json")

    def _load(self, name: str) -> dict:
        p = self.root / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _dump(self, name: str, obj) -> None:
        (self.root / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    # ---- lookups -----------------------------------------------------------
    def canonical(self, key: str) -> str:
        return self.aliases.get(key, key)

    def get(self, key: str) -> dict | None:
        return self.records.get(self.canonical(key))

    def is_done(self, key: str) -> bool:
        return self.canonical(key) in self.records

    def should_skip_failed(self, key: str) -> bool:
        return self.failures.get(key, {}).get("attempts", 0) >= MAX_ATTEMPTS

    # ---- writes (thread-safe, written immediately so a crash loses nothing) -
    def save(self, key: str, record: dict) -> None:
        record = {"media_key": key, "analyzed_at": now_iso(), **record}
        with self._lock:
            (self.dir / f"{slug(key)}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            self.records[key] = record
            if record.get("sha256"):
                self.by_sha[record["sha256"]] = key
            if key in self.failures:
                del self.failures[key]
                self._dump("failures.json", self.failures)
            if key in self.aliases:  # its former twin failed, so this one was watched itself
                del self.aliases[key]
                self._dump("aliases.json", self.aliases)

    def claim(self, sha256: str, key: str) -> str | None:
        """Atomically register `key` as the owner of this file content.

        Returns the key that already owns identical content (done or in progress), else None.
        """
        with self._lock:
            owner = self.by_sha.get(sha256)
            if owner and owner != key:
                return owner
            self.by_sha[sha256] = key
            return None

    def alias(self, key: str, target: str) -> None:
        with self._lock:
            self.aliases[key] = target
            self._dump("aliases.json", self.aliases)
            self.failures.pop(key, None)
            self._dump("failures.json", self.failures)

    def fail(self, key: str, reason: str) -> None:
        with self._lock:
            for sha in [s for s, k in self.by_sha.items() if k == key and k not in self.records]:
                del self.by_sha[sha]  # release the claim so a twin can be watched instead
            entry = self.failures.get(key, {"attempts": 0})
            entry.update(attempts=entry["attempts"] + 1, reason=reason[:500], last_at=now_iso())
            self.failures[key] = entry
            self._dump("failures.json", self.failures)
