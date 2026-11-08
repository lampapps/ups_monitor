import logging
import threading
import urllib.parse

import pytest

from app import settings
from app.kuma import KumaPusher
from app.poller import PollResult

URL = "http://kuma.lan:3001/api/push/SECRETTOKEN?status=up&msg=OK&ping="
OL = {"ups.status": "OL", "battery.charge": "100", "battery.runtime": "2400"}


def ok(ts):
    return PollResult(ts=ts, ok=True, status="OL", values=OL)


def fail(ts, kind="unreachable"):
    return PollResult(ts=ts, ok=False, error_kind=kind, error="timeout")


class Env:
    now = 1000.0
    urls = []
    error = None

    def clock(self):
        return self.now

    def opener(self, url, timeout):
        self.urls.append(url)
        if self.error:
            raise self.error


@pytest.fixture
def env():
    e = Env()
    e.urls = []
    return e


def pusher(env, **kw):
    return KumaPusher(URL, 60, 5, clock=env.clock, opener=env.opener, **kw)


def test_rejects_non_http_urls():
    for bad in ("file:///etc/passwd", "ftp://x/y", "nonsense", ""):
        with pytest.raises(ValueError):
            KumaPusher(bad, 60, 5)


def test_nothing_pushed_before_first_poll(env):
    p = pusher(env)
    assert p.status() is None and p.push_once() is None and env.urls == []


def test_up_with_summary(env):
    p = pusher(env)
    p.observe(ok(1000))
    assert p.status() == ("up", "UPS OL, charge 100%, runtime 40 min")


def test_on_battery_is_still_an_up_app(env):
    p = pusher(env)
    p.observe(PollResult(ts=1000, ok=True, status="OB DISCHRG", values={**OL, "ups.status": "OB DISCHRG"}))
    assert p.status()[0] == "up" and "OB DISCHRG" in p.status()[1]


def test_failures_down_only_after_grace(env):
    p = pusher(env)
    p.observe(fail(1000))
    p.observe(fail(1020))
    assert p.status()[0] == "up"
    env.now = 1031
    p.observe(fail(1031, "refused"))
    status, msg = p.status()
    assert status == "down" and "refused" in msg
    p.observe(ok(1036))
    env.now = 1036
    assert p.status()[0] == "up"


def test_stale_poll_result_is_down(env):
    p = pusher(env)
    p.observe(ok(1000))
    env.now = 1000 + settings.KUMA_STALE_MIN_S + 1
    status, msg = p.status()
    assert status == "down" and "no poll result" in msg


def test_url_keeps_token_path_and_replaces_query(env):
    parts = urllib.parse.urlsplit(pusher(env).build_url("down", "a b&c=d"))
    assert parts.netloc == "kuma.lan:3001" and parts.path == "/api/push/SECRETTOKEN"
    assert urllib.parse.parse_qs(parts.query) == {"status": ["down"], "msg": ["a b&c=d"]}


def test_push_once_sends_and_survives_errors_without_logging_token(env, caplog):
    p = pusher(env)
    p.observe(ok(1000))
    assert p.push_once() is True and "status=up" in env.urls[0]
    env.error = OSError(f"cannot reach {URL}")
    with caplog.at_level(logging.WARNING):
        assert p.push_once() is False
        assert p.push_once() is False
    assert "SECRETTOKEN" not in caplog.text
    assert len([r for r in caplog.records if "Kuma push failed" in r.message]) == 1
    env.error = None
    assert p.push_once() is True


def test_run_pushes_periodically_until_stopped(env):
    stop = threading.Event()
    p = KumaPusher(URL, 0.01, 5, clock=env.clock, opener=lambda u, t: env.urls.append(u) or (len(env.urls) >= 3 and stop.set()))
    p.observe(ok(1000))
    env.now = 1000
    t = threading.Thread(target=p.run, args=(stop,))
    t.start()
    t.join(5)
    assert not t.is_alive() and len(env.urls) >= 3
