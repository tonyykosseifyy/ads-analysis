"""GeminiAnalyzer against a fake client shaped like google-genai's (upload -> poll -> generate -> delete)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import aca.gemini as g

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample_analysis.json").read_text())


class FakeFiles:
    def __init__(self):
        self.polls, self.deleted = 0, []

    def upload(self, file, config):
        return SimpleNamespace(name="files/abc", state=SimpleNamespace(name="PROCESSING"))

    def get(self, name):
        self.polls += 1
        return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE" if self.polls >= 2 else "PROCESSING"))

    def delete(self, name):
        self.deleted.append(name)


class FakeModels:
    def __init__(self, replies):
        self.replies, self.configs = list(replies), []

    def generate_content(self, model, contents, config):
        self.configs.append(config)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r, candidates=[])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda s: None)


def make(replies):
    client = SimpleNamespace(files=FakeFiles(), models=FakeModels(replies))
    return g.GeminiAnalyzer("k", "gemini-3.8-flash", client=client), client


def test_waits_for_processing_then_parses_fenced_json(tmp_path):
    a, client = make(["```json\n" + json.dumps(SAMPLE, ensure_ascii=False) + "\n```"])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    out = a.analyze(f, context="Product(s): Blowout Boost")
    assert out["language"]["arabic_dialect"] == "Lebanese"
    assert client.files.polls == 2 and client.files.deleted == ["files/abc"]
    assert client.models.configs[0].response_json_schema is not None


def test_retries_then_falls_back_to_prompt_only_schema(tmp_path):
    a, client = make([RuntimeError("400 schema too complex"), "not json", json.dumps(SAMPLE)])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert a.analyze(f)["hook"]["hook_type"] == "expert_or_hairdresser"
    assert client.models.configs[2].response_json_schema is None  # third try: schema sent in the prompt instead


def test_incomplete_json_is_rejected_and_file_still_deleted(tmp_path):
    a, client = make([json.dumps({"asset_type": "video"})] * 4)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(g.GeminiError):
        a.analyze(f)
    assert client.files.deleted == ["files/abc"]


def test_model_with_zero_quota_fails_fast(tmp_path):
    a, client = make([RuntimeError("429 RESOURCE_EXHAUSTED ... limit: 0, model: gemini-4-argon")])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(g.ModelUnavailable):
        a.analyze(f)
    assert len(client.models.configs) == 1


def test_rate_limits_wait_the_suggested_delay_without_using_attempts(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(g.time, "sleep", lambda s: waits.append(s))
    rl = RuntimeError("429 RESOURCE_EXHAUSTED limit: 5 ... 'retryDelay': '37s'")
    a, client = make([rl, rl, rl, rl, rl, json.dumps(SAMPLE)])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert a.analyze(f)["asset_type"] == "video"
    assert 38.0 in waits
    assert client.models.configs[-1].response_json_schema is not None  # still on the first real attempt


def test_daily_quota_used_up_stops_immediately(tmp_path):
    a, client = make([RuntimeError("429 RESOURCE_EXHAUSTED limit: 20, description: Generate content requests per day limit exceeded on Free Tier")])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(g.ModelUnavailable, match="daily"):
        a.analyze(f)
    assert len(client.models.configs) == 1


def test_high_demand_503_backs_off_and_recovers(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(g.time, "sleep", lambda s: waits.append(s))
    busy = RuntimeError("503 UNAVAILABLE This model is currently experiencing high demand")
    a, _ = make([busy, busy, busy, busy, busy, json.dumps(SAMPLE)])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert a.analyze(f)["asset_type"] == "video"
    assert [w for w in waits if w >= 30] == [30, 60, 90, 120, 120]  # ignores the 4s upload polls
