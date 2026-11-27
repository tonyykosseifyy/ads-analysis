"""Tag each ad's primary texts and headlines (angle, language, hook style, offer) so copy can be compared
the same way as the videos. One cheap text call per unique copy set; results are cached in data/copy/."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .schema import ANGLES, HOOK_TYPES, _arr, _bool, _enum, _num, _obj, _str

log = logging.getLogger(__name__)

COPY_SCHEMA = _obj({
    "language": _enum(["ar", "en", "mixed", "arabizi", "fr", "none"]),
    "main_angle": _enum(ANGLES),
    "secondary_angles": _arr(_enum(ANGLES)),
    "opening_line": _str,
    "opening_style": _enum(HOOK_TYPES),
    "mentions_offer": _bool,
    "offer": _str,
    "social_proof": _str,
    "cta": _str,
    "tone": _str,
    "variation_count": _num,
})

PROMPT = """You are tagging the ad text of a Meta ad for WavyTalk Lebanon (hair tools). Below are the primary text
variations and headline variations the ad rotates. Describe them as they are; do not judge or rewrite.
- language: the language(s) the copy is written in (arabizi = Arabic in Latin letters/numbers).
- main_angle / secondary_angles: what the copy sells on.
- opening_line: the first line of the first primary text, verbatim.
- opening_style: what kind of opening that line is.
- offer: any discount, code, free delivery or deal stated, verbatim ("" if none).
- social_proof: any numbers sold, reviews or endorsements stated ("" if none).
- cta: the call to action, verbatim.
- variation_count: how many primary texts + headlines there are.
Return only JSON matching the schema.

PRIMARY TEXTS:
{bodies}

HEADLINES:
{titles}
"""


def copy_key(copy: dict) -> str:
    raw = json.dumps([copy.get("primary_texts", []), copy.get("headlines", [])], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def local_stats(copy: dict) -> dict:
    texts = copy.get("primary_texts", [])
    first = texts[0] if texts else ""
    emoji = re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", " ".join(texts))
    return {"copy_words": len(first.split()), "copy_emoji_count": len(emoji),
            "copy_text_variations": len(texts), "copy_headline_variations": len(copy.get("headlines", []))}


def analyze_copies(ads: list[dict], analyzer, cache_dir: Path, concurrency: int = 6) -> dict[str, dict]:
    """Returns copy_key -> tags for every ad's copy (cached across runs)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for ad in ads:
        c = ad["copy"]
        if c.get("primary_texts") or c.get("headlines"):
            wanted.setdefault(copy_key(c), c)
    done = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in cache_dir.glob("*.json")}
    todo = [(k, c) for k, c in wanted.items() if k not in done]
    log.info("Ad copy: %s unique sets, %s new to tag", len(wanted), len(todo))

    def work(item):
        key, c = item
        prompt = PROMPT.format(bodies="\n---\n".join(c.get("primary_texts", [])) or "(none)",
                               titles="\n".join(c.get("headlines", [])) or "(none)")
        try:
            tags = analyzer.analyze_text(prompt, COPY_SCHEMA, label=f"copy {key}")
        except Exception as e:  # noqa: BLE001 - copy tags are optional (and stop at the budget cap); blank on failure
            log.warning("Copy tagging failed for %s: %s", key, str(e)[:200])
            return key, None
        (cache_dir / f"{key}.json").write_text(json.dumps(tags, ensure_ascii=False, indent=1), encoding="utf-8")
        return key, tags

    if todo and analyzer is not None:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for key, tags in pool.map(work, todo):
                if tags:
                    done[key] = tags
    return done


def copy_columns(copy: dict, tags: dict | None) -> dict:
    t = tags or {}
    return {
        "copy_language": t.get("language", ""), "copy_main_angle": t.get("main_angle", ""),
        "copy_secondary_angles": " | ".join(t.get("secondary_angles", []) or []),
        "copy_opening_line": t.get("opening_line", ""), "copy_opening_style": t.get("opening_style", ""),
        "copy_offer": t.get("offer", ""), "copy_social_proof": t.get("social_proof", ""), "copy_cta": t.get("cta", ""),
        "copy_tone": t.get("tone", ""), **local_stats(copy),
    }
