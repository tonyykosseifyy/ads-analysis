"""MetaClient request building, async report polling, pagination and retry, with a fake HTTP session."""
import pytest

import aca.meta as m
from aca.config import Settings


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = script, []

    def request(self, method, url, params=None, timeout=None):
        self.calls.append((method, url, dict(params or {})))
        for match, reply in self.script:
            if match(method, url, params or {}):
                return reply.pop(0) if isinstance(reply, list) else reply
        raise AssertionError(f"unexpected {method} {url} {params}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)


def client(script):
    s = Settings(meta_access_token="TOKEN", meta_ad_account_id="123", meta_api_version="v24.0")
    sess = FakeSession(script)
    return m.MetaClient(s, session=sess), sess


def test_async_insights_poll_and_paginate():
    c, sess = client([
        (lambda me, u, p: me == "POST" and u.endswith("/act_123/insights"), Resp(200, {"report_run_id": "RUN1"})),
        (lambda me, u, p: u.endswith("/RUN1") and "async_status" in p.get("fields", ""),
         [Resp(200, {"async_status": "Job Running"}), Resp(200, {"async_status": "Job Completed"})]),
        (lambda me, u, p: u.endswith("/RUN1/insights"),
         Resp(200, {"data": [{"ad_id": "1"}], "paging": {"next": "https://graph.facebook.com/v24.0/RUN1/page2?access_token=TOKEN"}})),
        (lambda me, u, p: "page2" in u, Resp(200, {"data": [{"ad_id": "2"}]})),
    ])
    rows = c.ad_insights()
    assert [r["ad_id"] for r in rows] == ["1", "2"]
    post = sess.calls[0]
    assert post[2]["level"] == "ad" and post[2]["date_preset"] == "maximum" and post[2]["access_token"] == "TOKEN"
    assert "access_token" not in sess.calls[-1][2]  # next-page URLs already carry the token


def test_rate_limit_is_retried():
    c, _ = client([(lambda me, u, p: u.endswith("/555"),
                    [Resp(400, {"error": {"code": 17, "message": "User request limit reached"}}),
                     Resp(200, {"id": "555", "source": "https://cdn/x.mp4"})])])
    assert c.video_info("555")["source"] == "https://cdn/x.mp4"


def test_permission_error_raises():
    c, _ = client([(lambda me, u, p: True, Resp(400, {"error": {"code": 10, "message": "Permission denied"}}))])
    with pytest.raises(m.MetaError, match="Permission denied"):
        c.video_info("1")


def test_batch_ad_lookup_falls_back_to_single_on_error():
    c, _ = client([
        (lambda me, u, p: "ids" in p, Resp(400, {"error": {"code": 100, "message": "Unsupported get request"}})),
        (lambda me, u, p: u.endswith("/a1"), Resp(200, {"id": "a1"})),
        (lambda me, u, p: u.endswith("/a2"), Resp(400, {"error": {"code": 100, "message": "deleted"}})),
    ])
    assert c.ads_by_ids(["a1", "a2"]) == {"a1": {"id": "a1"}}


def test_image_hash_lookup_sends_json_array():
    c, sess = client([(lambda me, u, p: u.endswith("/act_123/adimages"),
                       Resp(200, {"data": [{"hash": "h1", "url": "https://cdn/h1.jpg"}]}))])
    assert c.image_urls(["h1"]) == {"h1": "https://cdn/h1.jpg"}
    assert sess.calls[0][2]["hashes"] == '["h1"]'
