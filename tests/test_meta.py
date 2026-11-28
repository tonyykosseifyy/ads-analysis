"""MetaClient request building, async report polling, pagination and retry, with a fake HTTP session."""
import pytest

import aca.meta as m
from aca.config import Settings


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body = status, body
        self.text = str(body)
        self.headers = headers or {}

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


class Clock:
    """Fake time: sleeping advances it instantly, and the sleeps are recorded."""
    def __init__(self):
        self.now, self.slept = 1_000_000.0, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(m.time, "sleep", c.sleep)
    monkeypatch.setattr(m.time, "time", c.time)
    return c


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


def test_ads_listed_from_account_with_all_statuses_then_missing_fetched_singly():
    c, sess = client([
        (lambda me, u, p: u.endswith("/act_123/ads"),
         Resp(200, {"data": [{"id": "a1"}, {"id": "other"}],
                    "paging": {"next": "https://graph.facebook.com/v24.0/act_123/ads?after=X&access_token=TOKEN"}})),
        (lambda me, u, p: "after=X" in u, Resp(200, {"data": [{"id": "a2"}]})),
        (lambda me, u, p: u.endswith("/a3"), Resp(200, {"id": "a3"})),
        (lambda me, u, p: u.endswith("/a4"), Resp(400, {"error": {"code": 100, "message": "deleted"}})),
    ])
    assert c.ads_by_ids(["a1", "a2", "a3", "a4"]) == {"a1": {"id": "a1"}, "a2": {"id": "a2"}, "a3": {"id": "a3"}}
    listing = sess.calls[0][2]
    assert "ARCHIVED" in listing["filtering"] and "DELETED" in listing["filtering"]
    assert not any("ids" in call[2] for call in sess.calls)


def test_parameter_errors_are_not_retried_even_on_500():
    c, sess = client([(lambda me, u, p: True,
                       Resp(500, {"error": {"code": 100, "message": "The ids query parameter is deprecated"}}))])
    with pytest.raises(m.MetaError, match="deprecated"):
        c.video_info("1")
    assert len(sess.calls) == 1


def test_image_hash_lookup_sends_json_array():
    c, sess = client([(lambda me, u, p: u.endswith("/act_123/adimages"),
                       Resp(200, {"data": [{"hash": "h1", "url": "https://cdn/h1.jpg"}]}))])
    assert c.image_urls(["h1"]) == {"h1": "https://cdn/h1.jpg"}
    assert sess.calls[0][2]["hashes"] == '["h1"]'


def test_app_rate_limit_pauses_all_calls_then_resumes(clock):
    c, sess = client([(lambda me, u, p: u.endswith("/555"),
                       [Resp(400, {"error": {"code": 4, "message": "Application request limit reached"}}),
                        Resp(400, {"error": {"code": 4, "message": "Application request limit reached"}}),
                        Resp(200, {"id": "555", "source": "https://cdn/x.mp4"})])])
    assert c.video_info("555")["source"] == "https://cdn/x.mp4"
    assert sum(clock.slept) >= 60 + 120  # shared pause grows: 1 min, then 2 min
    assert len(sess.calls) == 3


def test_usage_header_near_limit_slows_down_before_being_blocked(clock):
    c, _ = client([(lambda me, u, p: u.endswith("/1"),
                    Resp(200, {"id": "1"}, {"x-app-usage": '{"call_count": 97, "total_time": 10, "total_cputime": 5}'})),
                   (lambda me, u, p: u.endswith("/2"), Resp(200, {"id": "2"}))])
    c.get("1")
    c.get("2")
    assert sum(clock.slept) >= 300
    assert m._max_usage('{"123": [{"call_count": 40, "total_time": 91}]}') == 91


def test_prefetch_lists_account_videos_and_images_so_no_per_file_calls(clock):
    c, sess = client([
        (lambda me, u, p: u.endswith("/act_123/advideos"),
         Resp(200, {"data": [{"id": "v1", "source": "https://cdn/v1.mp4", "length": 30}]})),
        (lambda me, u, p: u.endswith("/act_123/adimages"), Resp(200, {"data": [{"hash": "h1", "url": "https://cdn/h1.jpg"}]})),
    ])
    c.prefetch_media()
    n = len(sess.calls)
    assert c.video_info("v1")["source"] == "https://cdn/v1.mp4"
    assert c.image_urls(["h1"]) == {"h1": "https://cdn/h1.jpg"}
    assert len(sess.calls) == n  # served from the prefetch
    clock.now += 46 * 60  # file links expire: an old entry is fetched again
    sess.script.append((lambda me, u, p: u.endswith("/v1"), Resp(200, {"id": "v1", "source": "https://cdn/v1b.mp4"})))
    assert c.video_info("v1")["source"] == "https://cdn/v1b.mp4"


def test_page_video_file_link_comes_from_the_page_token(clock):
    c, sess = client([
        (lambda me, u, p: u.endswith("/me/accounts"),
         Resp(200, {"data": [{"id": "P1", "name": "Wavytalk Lb", "access_token": "PAGETOKEN"}]})),
        (lambda me, u, p: u.endswith("/777") and p.get("access_token") == "PAGETOKEN",
         Resp(200, {"id": "777", "source": "https://cdn/777.mp4"})),
        (lambda me, u, p: u.endswith("/777"), Resp(200, {"id": "777"})),  # business token: no file link
    ])
    assert "source" not in c.video_info("777")
    assert c.video_info_as_page("777")["source"] == "https://cdn/777.mp4"
    assert c.video_info("777")["source"] == "https://cdn/777.mp4"  # cached for the download
