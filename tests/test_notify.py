import threading

import pytest

from app import notify
from app.config import SmtpConfig
from app.events import Event
from app.notify import Notifier, build_message, send_message, wants

CFG = dict(
    enabled=True, host="smtp.test", port=587, sender="ups@test", recipients=["a@test", "b@test"],
    username_env="U", password_env="P",
)


def smtp_cfg(**kw):
    return SmtpConfig(**{**CFG, **kw})


def ev(type_="on_battery", severity="warning", message="Power lost", **data):
    return Event(1_800_000_000, type_, severity, message, data)


@pytest.mark.parametrize(
    "type_, severity, expected",
    [
        ("on_battery", "warning", True),
        ("low_battery", "critical", True),
        ("shutdown_started", "critical", True),
        ("shutdown_step", "critical", True),
        ("shutdown_step", "info", False),
        ("self_test", "info", False),
        ("self_test", "warning", True),
        ("power_restored", "info", True),
        ("ups_recovered", "info", True),
        ("preflight_failed", "warning", True),
        ("preflight_ok", "info", True),
        ("monitor_started", "info", False),
        ("monitor_stopped", "info", False),
        ("conn_refused", "warning", False),
        ("notify_failed", "warning", False),
        ("some_future_event", "critical", False),
    ],
)
def test_wants_defaults(type_, severity, expected):
    assert wants(ev(type_, severity), smtp_cfg()) is expected


def test_events_can_be_switched_both_ways():
    cfg = smtp_cfg(events={"on_battery": False, "shutdown_step_ok": True, "self_test_ok": True, "conn_refused": True})
    assert wants(ev("on_battery", "warning"), cfg) is False
    assert wants(ev("shutdown_step", "info"), cfg) is True
    assert wants(ev("self_test", "info"), cfg) is True
    assert wants(ev("conn_refused", "warning"), cfg) is True
    assert wants(ev("low_battery", "critical"), cfg) is True  # untouched keys keep their defaults


def test_notify_failed_cannot_be_enabled():
    with pytest.raises(ValueError, match="unknown smtp.events"):
        smtp_cfg(events={"notify_failed": True})


def test_unknown_event_name_is_rejected():
    with pytest.raises(ValueError, match="on_batery"):
        smtp_cfg(events={"on_batery": True})


def test_every_default_key_is_reachable_from_some_event():
    assert set(notify.settings.NOTIFY_EVENT_DEFAULTS) >= {notify.event_key(ev(t, s)) for t, s in [
        ("self_test", "info"), ("self_test", "warning"), ("shutdown_step", "info"), ("shutdown_step", "critical"),
    ]}


def test_message_headers_and_body():
    msg = build_message(smtp_cfg(), ev(battery_runtime="300"), "pi5-1")
    assert msg["From"] == "ups@test" and msg["To"] == "a@test, b@test"
    assert msg["Subject"] == "[ups_monitor] WARNING: Power lost"
    body = msg.get_content()
    assert "Time:   2027-01-15" in body and "pi5-1" in body and '"battery_runtime": "300"' in body


def test_newlines_cannot_inject_headers():
    msg = build_message(smtp_cfg(), ev(message="ssh failed\r\nBcc: evil@test\nmore"), "h")
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert msg["Bcc"] is None


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.args = (host, port, timeout)
        self.context = context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")
        assert context is not None  # certificate checking stays on

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append("send")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_send_starttls_with_login(fake_smtp):
    cfg = smtp_cfg()
    send_message(cfg, {"U": "user", "P": "pw"}, build_message(cfg, ev(), "h"))
    (smtp,) = fake_smtp.instances
    assert smtp.args == ("smtp.test", 587, 10.0)
    assert smtp.calls == ["starttls", ("login", "user", "pw"), "send", "quit"]


def test_send_ssl_has_tls_context_and_no_starttls(fake_smtp):
    cfg = smtp_cfg(security="ssl", port=465)
    send_message(cfg, {"U": "u", "P": "p"}, build_message(cfg, ev(), "h"))
    smtp = fake_smtp.instances[0]
    assert smtp.context is not None and "starttls" not in smtp.calls


def test_send_plain_without_credentials(fake_smtp):
    cfg = smtp_cfg(security="none", username_env=None, password_env=None)
    send_message(cfg, {}, build_message(cfg, ev(), "h"))
    assert fake_smtp.instances[0].calls == ["send", "quit"]


# ---------------------------------------------------------------- Notifier


def make_notifier(send, enabled=True, retry_delays=(0, 0)):
    failures = []
    n = Notifier(
        smtp_cfg(enabled=enabled), {}, lambda *a: failures.append(a), send=send,
        retry_delays=retry_delays, hostname="h",
    )
    n.start()
    return n, failures


def test_only_wanted_events_are_sent_and_flush_waits():
    sent = []
    n, failures = make_notifier(lambda m: sent.append(m["Subject"]))
    n.submit(ev("monitor_started", "info"))
    n.submit(ev("on_battery", "warning", "Power lost"))
    n.submit(ev("power_restored", "info", "Power back"))
    assert n.flush(5)
    assert sent == ["[ups_monitor] WARNING: Power lost", "[ups_monitor] INFO: Power back"]
    assert failures == []
    n.stop()


def test_disabled_notifier_does_nothing():
    sent = []
    n, _ = make_notifier(sent.append, enabled=False)
    n.submit(ev())
    assert n.flush(0.1) and sent == []


def test_retries_then_succeeds():
    attempts = []

    def flaky(msg):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("network down")

    n, failures = make_notifier(flaky)
    n.submit(ev())
    assert n.flush(5)
    assert len(attempts) == 3 and failures == []
    n.stop()


def test_gives_up_and_records_notify_failed_without_looping():
    def broken(msg):
        raise OSError("no route")

    n, failures = make_notifier(broken)
    n.submit(ev("low_battery", "critical"))
    assert n.flush(5)
    assert len(failures) == 1
    _, type_, severity, message, data = failures[0]
    assert (type_, severity) == ("notify_failed", "warning")
    assert "3 attempts" in message and "no route" in message and data == {"event_type": "low_battery"}
    n.submit(Event(0, type_, severity, message))  # a failure event must never be mailed
    assert n.flush(0.2)
    n.stop()


def test_flush_times_out_when_sending_hangs_and_submit_never_blocks():
    release = threading.Event()
    n, _ = make_notifier(lambda m: release.wait(5))
    n.submit(ev())
    assert n.flush(0.1) is False
    n.submit(ev())  # still returns immediately
    release.set()
    assert n.flush(5)
    n.stop()


def test_queue_full_drops_instead_of_blocking(monkeypatch):
    monkeypatch.setattr(notify.settings, "NOTIFY_QUEUE_MAX", 2)
    release = threading.Event()
    n, _ = make_notifier(lambda m: release.wait(5))
    for _ in range(10):
        n.submit(ev())
    assert n._pending <= 3  # one in flight plus a bounded queue
    release.set()
    assert n.flush(5)
    n.stop()
