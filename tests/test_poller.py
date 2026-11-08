import threading

import pytest

from app import db, nut
from app.config import UpsConfig
from app.poller import Poller, build_sample

VARS = {
    "ups.status": "OL",
    "battery.charge": "100",
    "battery.runtime": "2389.0",
    "input.voltage": "122.3",
    "ups.model": "TOWER_1000VA_120V",
    "ups.load": "bogus",
}


class Script:
    """Fetch stand-in that replays a list of dicts or NutErrors."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return dict(step)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def make(conn, *steps, clock=None):
    ticks = iter(range(1000, 10_000))
    return Poller(UpsConfig(host="x"), conn, fetch=Script(*steps), clock=clock or (lambda: next(ticks)))


def refused():
    return nut.NutError(nut.REFUSED, "connection refused (UPS may be at its client limit)")


def test_build_sample_converts_types_and_tolerates_bad_values():
    s = build_sample(5, VARS)
    assert s["status"] == "OL"
    assert s["battery_runtime"] == 2389 and isinstance(s["battery_runtime"], int)
    assert s["input_voltage"] == 122.3
    assert s["load_pct"] is None
    assert s["temperature"] is None


def test_successful_poll_stores_sample_and_info(conn):
    p = make(conn, VARS)
    r = p.poll_once()
    assert r.ok and r.status == "OL" and not r.on_battery
    row = conn.execute("SELECT * FROM samples").fetchone()
    assert (row["ts"], row["status"], row["battery_runtime"]) == (1000, "OL", 2389)
    info = dict(conn.execute("SELECT key, value FROM ups_info").fetchall())
    assert info["ups.model"] == "TOWER_1000VA_120V"


def test_info_only_rewritten_on_change(conn):
    p = make(conn, VARS)
    p.poll_once()
    first = conn.execute("SELECT updated_ts FROM ups_info WHERE key='ups.model'").fetchone()[0]
    p.poll_once()
    assert conn.execute("SELECT updated_ts FROM ups_info WHERE key='ups.model'").fetchone()[0] == first


def test_failed_poll_is_a_gap_with_no_sample(conn):
    p = make(conn, nut.NutError(nut.UNREACHABLE, "timeout"))
    r = p.poll_once()
    assert not r.ok and r.error_kind == nut.UNREACHABLE and r.status is None
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_missing_status_counts_as_failed_poll(conn):
    p = make(conn, {"battery.charge": "50"})
    r = p.poll_once()
    assert not r.ok and r.error_kind == nut.PROTOCOL
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0


def test_refusal_event_logged_once_per_streak(conn):
    p = make(conn, refused(), refused(), VARS, refused())
    for _ in range(4):
        p.poll_once()
    rows = conn.execute("SELECT type, severity, message FROM events").fetchall()
    assert [r["type"] for r in rows] == ["conn_refused", "conn_refused"]
    assert "client limit" in rows[0]["message"]


def test_database_failure_does_not_stop_polling(conn):
    conn.close()  # every write now raises sqlite3.ProgrammingError
    p = make(conn, VARS)
    assert p.poll_once().ok


def test_no_database_at_all(conn):
    assert make(None, VARS).poll_once().ok


def test_listener_exception_is_contained(conn):
    p = make(conn, VARS)
    seen = []
    p.listeners += [lambda r: 1 / 0, seen.append]
    p.poll_once()
    assert len(seen) == 1


def test_faster_polling_while_on_battery(conn):
    p = make(conn, {**VARS, "ups.status": "OB DISCHRG"}, nut.NutError(nut.UNREACHABLE, "x"), VARS)
    assert p.next_delay() == p.cfg.poll_interval_s
    r = p.poll_once()
    assert r.on_battery and p.next_delay() == p.cfg.poll_interval_on_battery_s
    p.poll_once()  # a gap keeps the last known state
    assert p.next_delay() == p.cfg.poll_interval_on_battery_s
    p.poll_once()
    assert p.next_delay() == p.cfg.poll_interval_s


def test_calibration_status_is_not_flagged_on_battery_when_ol(conn):
    r = make(conn, {**VARS, "ups.status": "OL CAL"}).poll_once()
    assert not r.on_battery


def test_run_loop_polls_until_stopped(conn):
    p = make(conn, VARS)
    stop = threading.Event()
    delays = []

    def wait(delay):
        delays.append(delay)
        return len(delays) >= 3

    p.run(stop, wait=wait)
    assert p._fetch.calls == 3
    assert all(0 <= d <= p.cfg.poll_interval_s for d in delays)


def test_sample_rows_are_replaced_not_duplicated(conn):
    p = make(conn, VARS, clock=lambda: 42)
    p.poll_once()
    p.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_heartbeat_written_for_success_and_failure(conn):
    p = make(conn, VARS, nut.NutError(nut.UNREACHABLE, "x"))
    p.poll_once()
    row = conn.execute("SELECT value, updated_ts FROM monitor_state WHERE key = 'last_poll'").fetchone()
    assert row["updated_ts"] == 1000 and '"ok": true' in row["value"]
    p.poll_once()
    row = conn.execute("SELECT value, updated_ts FROM monitor_state WHERE key = 'last_poll'").fetchone()
    assert row["updated_ts"] == 1001 and '"ok": false' in row["value"] and "unreachable" in row["value"]


def test_refusal_goes_through_emit_when_provided(conn):
    seen = []
    ticks = iter(range(1000, 2000))
    p = Poller(UpsConfig(host="x"), conn, fetch=Script(refused()), clock=lambda: next(ticks),
               emit=lambda *a: seen.append(a))
    p.poll_once()
    p.poll_once()
    assert [a[1] for a in seen] == ["conn_refused"]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
