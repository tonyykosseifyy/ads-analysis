"""Send one video/image to Gemini and get back the structured description."""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from pathlib import Path

from .schema import ANALYSIS_SCHEMA, REQUIRED_TOP_LEVEL

log = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analysis_prompt.md"


class GeminiError(RuntimeError):
    pass


class GeminiAnalyzer:
    def __init__(self, api_key: str, model: str, client=None):
        if client is None:
            from google import genai  # imported lazily so tests run without the SDK
            client = genai.Client(api_key=api_key)
        self.client = client
        self.model = model
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def analyze(self, path: Path, context: str = "") -> dict:
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        uploaded = self.client.files.upload(file=str(path), config={"mime_type": mime})
        try:
            uploaded = self._wait_active(uploaded)
            prompt = self.prompt + (f"\n\n## Context from the ad account (not visible in the asset)\n{context}" if context else "")
            last_err: Exception | None = None
            for attempt in range(4):
                use_schema = attempt < 2  # if the schema itself is rejected, fall back to prompt-only JSON
                try:
                    text = self._generate(uploaded, prompt, use_schema)
                    result = _parse_json(text)
                    missing = [k for k in REQUIRED_TOP_LEVEL if k not in result]
                    if missing:
                        raise GeminiError(f"Response missing fields: {missing}")
                    return result
                except Exception as e:  # noqa: BLE001 - retry any API/parse failure
                    last_err = e
                    wait = 10 * (attempt + 1)
                    log.warning("Gemini attempt %s failed for %s: %s (retry in %ss)", attempt + 1, path.name, e, wait)
                    time.sleep(wait)
            raise GeminiError(f"Gemini failed for {path.name}: {last_err}")
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:  # noqa: BLE001 - cleanup is best-effort; files expire after 48h anyway
                pass

    def _wait_active(self, f, timeout_s: int = 600):
        start = time.time()
        while _state(f) == "PROCESSING":
            if time.time() - start > timeout_s:
                raise GeminiError("Gemini file processing timed out")
            time.sleep(4)
            f = self.client.files.get(name=f.name)
        if _state(f) != "ACTIVE":
            raise GeminiError(f"Gemini could not process the file (state {_state(f)})")
        return f

    def _generate(self, uploaded, prompt: str, use_schema: bool) -> str:
        from google.genai import types
        if use_schema:
            config = types.GenerateContentConfig(
                temperature=0.2, response_mime_type="application/json", response_json_schema=ANALYSIS_SCHEMA,
            )
        else:
            prompt = prompt + "\n\nJSON schema to follow exactly:\n" + json.dumps(ANALYSIS_SCHEMA)
            config = types.GenerateContentConfig(temperature=0.2, response_mime_type="application/json")
        resp = self.client.models.generate_content(model=self.model, contents=[uploaded, prompt], config=config)
        if not getattr(resp, "text", None):
            raise GeminiError(f"Empty response (finish reason: {_finish_reason(resp)})")
        return resp.text


def _state(f) -> str:
    s = getattr(f, "state", None)
    return getattr(s, "name", None) or str(s or "")


def _finish_reason(resp) -> str:
    try:
        return str(resp.candidates[0].finish_reason)
    except Exception:  # noqa: BLE001
        return "unknown"


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise GeminiError("No JSON object in response")
    return json.loads(text[start:end + 1])
