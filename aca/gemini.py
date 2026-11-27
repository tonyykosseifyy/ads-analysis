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


def make_analyzer(settings, model: str) -> "GeminiAnalyzer":
    """Build the analyzer for the configured backend (Vertex AI or AI Studio key)."""
    settings.require_gemini()
    if settings.gemini_backend == "vertex":
        return GeminiAnalyzer(None, model, vertex={"project": settings.gcp_project, "location": settings.gcp_location,
                                                   "bucket": settings.gcs_bucket})
    return GeminiAnalyzer(settings.gemini_api_key, model)


class ModelUnavailable(GeminiError):
    """The API key's plan has no quota at all for this model (e.g. flagship on the free tier)."""


class GeminiAnalyzer:
    def __init__(self, api_key: str | None, model: str, client=None, vertex: dict | None = None, bucket=None):
        """vertex={"project":..., "location":..., "bucket":...} routes calls through Vertex AI (Google Cloud
        billing, so Cloud credits apply). Videos are then passed as gs:// files from a private bucket instead
        of the AI Studio Files API, which Vertex doesn't have."""
        self.vertex = vertex
        if client is None:
            from google import genai  # imported lazily so tests run without the SDK
            if vertex:
                client = genai.Client(vertexai=True, project=vertex["project"], location=vertex.get("location", "global"))
            else:
                client = genai.Client(api_key=api_key)
        if vertex and bucket is None:
            from google.cloud import storage
            bucket = storage.Client(project=vertex["project"]).bucket(vertex["bucket"])
        self.client = client
        self.bucket = bucket
        self.model = model
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def analyze(self, path: Path, context: str = "") -> dict:
        if self.vertex:
            return self._analyze_vertex(path, context)
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        uploaded = self.client.files.upload(file=str(path), config={"mime_type": mime})
        try:
            uploaded = self._wait_active(uploaded)
            prompt = self.prompt + (f"\n\n## Context from the ad account (not visible in the asset)\n{context}" if context else "")
            return self._call([uploaded], prompt, ANALYSIS_SCHEMA, REQUIRED_TOP_LEVEL, path.name)
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:  # noqa: BLE001 - cleanup is best-effort; files expire after 48h anyway
                pass

    def _analyze_vertex(self, path: Path, context: str) -> dict:
        import uuid
        from google.genai import types
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        blob = self.bucket.blob(f"tmp/{uuid.uuid4().hex}-{path.name}")
        blob.upload_from_filename(str(path), content_type=mime)
        try:
            part = types.Part.from_uri(file_uri=f"gs://{self.bucket.name}/{blob.name}", mime_type=mime)
            prompt = self.prompt + (f"\n\n## Context from the ad account (not visible in the asset)\n{context}" if context else "")
            return self._call([part], prompt, ANALYSIS_SCHEMA, REQUIRED_TOP_LEVEL, path.name)
        finally:
            try:
                blob.delete()  # the bucket also deletes anything older than a day
            except Exception:  # noqa: BLE001
                pass

    def analyze_text(self, prompt: str, schema: dict, label: str = "text") -> dict:
        """Text-only call with the same retry rules (used for the ad copy)."""
        return self._call([], prompt, schema, list(schema.get("properties", {})), label)

    def _call(self, parts: list, prompt: str, schema: dict, required: list[str], label: str) -> dict:
        last_err: Exception | None = None
        attempt = rate_limited = 0
        while attempt < 4 and rate_limited < 8:
            use_schema = attempt < 2  # if the schema itself is rejected, fall back to prompt-only JSON
            try:
                text = self._generate(parts, prompt, use_schema, schema)
                result = _parse_json(text)
                missing = [k for k in required if k not in result]
                if missing:
                    raise GeminiError(f"Response missing fields: {missing}")
                return result
            except Exception as e:  # noqa: BLE001 - retry any API/parse failure
                last_err = e
                msg = str(e)
                if "limit: 0" in msg:  # the key's plan doesn't include this model: retrying can't help
                    raise ModelUnavailable(f"{self.model} is not available on this API key's plan") from e
                if "per day" in msg.lower() or "PerDay" in msg:  # daily cap used up: waiting minutes won't help
                    raise ModelUnavailable(f"{self.model}: daily request quota used up on this API key") from e
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    rate_limited += 1  # rate limits don't use up the normal attempts
                    wait = _retry_delay(msg) or 30
                elif "503" in msg or "UNAVAILABLE" in msg:  # Google-side "high demand": back off longer
                    rate_limited += 1
                    wait = min(120, 30 * rate_limited)
                else:
                    attempt += 1
                    wait = 10 * attempt
                log.warning("Gemini %s for %s: %s (retry in %ss)", "rate-limited" if "429" in msg else "failed",
                            label, msg[:160], wait)
                time.sleep(wait)
        raise GeminiError(f"Gemini failed for {label}: {last_err}")

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

    def _generate(self, parts: list, prompt: str, use_schema: bool, schema: dict = ANALYSIS_SCHEMA) -> str:
        from google.genai import types
        if use_schema:
            config = types.GenerateContentConfig(
                temperature=0.2, response_mime_type="application/json", response_json_schema=schema,
            )
        else:
            prompt = prompt + "\n\nJSON schema to follow exactly:\n" + json.dumps(schema)
            config = types.GenerateContentConfig(temperature=0.2, response_mime_type="application/json")
        resp = self.client.models.generate_content(model=self.model, contents=[*parts, prompt], config=config)
        if not getattr(resp, "text", None):
            raise GeminiError(f"Empty response (finish reason: {_finish_reason(resp)})")
        return resp.text


def _retry_delay(msg: str) -> float | None:
    """Google's 429s say how long to wait, e.g. "'retryDelay': '37s'" or "Please retry in 36.8s"."""
    import re
    m = re.search(r"retryDelay'?:\s*'?(\d+(?:\.\d+)?)s", msg) or re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
    return min(120.0, float(m.group(1)) + 1) if m else None


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
