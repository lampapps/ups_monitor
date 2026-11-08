from datetime import datetime, timezone

import pytest

from app import db
from app.events import EventDetector, fmt_duration, fmt_local
from app.poller import PollResult

OL = {"ups.status": "OL", "ups.test.result": "done and passed", "battery.charge": "100"}


def ok(ts, status="OL", **extra):
    return PollResult(ts=ts, ok=True, status=status, values={**OL, "ups.status": status, **extra})


def fail(ts, kind="unreachable"):
    return PollResult(ts=ts, ok=False, error_kind=kind, error="timeout")


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


@pytest.fixture
def det(conn):
    d = EventDetector(conn, unreachable_after_s=30)
    d.seen = []
    d.on_event.append(d.seen.append)
    return d


def types(det):
    return [e.type for e in det.seen]


def feed(det, *results):
    for r in results:
        det(r)


def test_formatting_helpers():
    ts = int(datetime(2026, 10, 28, 2, 52, 10, tzinfo=timezone.utc).timestamp())
    assert fmt_local(ts) == "2026-10-27 22:52:10 EDT"
    assert [fmt_duration(s) for s in (10, 200, 3900)] == ["10 s", "3 min 20 s", "1 h 5 min"]


def test_steady_online_makes_no_events(det):
    feed(det, *(ok(t) for t in range(0, 100, 5)))
    assert det.seen == []


def test_low_charge_after_test_is_not_a_fault(det):
    feed(det, ok(0, battery_charge="60"), ok(5, **{"battery.charge": "62"}))
    assert det.seen == []


def test_power_loss_and_restore(det, conn):
    feed(det, ok(0), ok(5, "OB DISCHRG"), ok(10, "OB DISCHRG"), ok(305, "OL CHRG"))
    assert types(det) == ["on_battery", "power_restored"]
    assert "5 min 0 s" in det.seen[1].message
    assert det.seen[0].severity == "warning"
    stored = [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert stored == ["on_battery", "power_restored"]


def test_gap_during_outage_does_not_duplicate_or_restore(det):
    feed(det, ok(0, "OB"), fail(5), fail(10), ok(15, "OB"))
    assert types(det) == ["on_battery"]
    feed(det, fail(20), ok(25, "OL"))
    assert types(det) == ["on_battery", "power_restored"]


def test_first_poll_on_battery_is_reported(det):
    feed(det, ok(0, "OB DISCHRG"))
    assert types(det) == ["on_battery"]


def test_self_test_is_not_a_power_loss(det):
    feed(det, ok(0), ok(5, "OB CAL"), ok(10, "OB CAL"), ok(15, "OL"))
    assert types(det) == ["self_test"]
    ev = det.seen[0]
    assert ev.severity == "info"
    assert ev.data["started_ts"] == 5 and ev.data["result"] == "done and passed"
    assert "10 s" in ev.message


def test_failed_self_test_is_a_warning(det):
    feed(det, ok(0, "OB CAL"), ok(10, "OL", **{"ups.test.result": "done and error"}))
    assert det.seen[0].severity == "warning"


def test_outage_starting_during_self_test(det):
    feed(det, ok(0), ok(5, "OB CAL"), ok(10, "OB"))
    assert types(det) == ["self_test", "on_battery"]


def test_flags_reported_once_per_appearance(det):
    feed(det, ok(0, "OL"), ok(5, "OB LB"), ok(10, "OB LB"), ok(15, "OL"), ok(20, "OL RB"), ok(25, "OL RB"))
    assert types(det) == ["on_battery", "low_battery", "power_restored", "replace_battery"]
    assert det.seen[1].severity == "critical"
    feed(det, ok(30, "OL"), ok(35, "OL RB"))
    assert types(det)[-1] == "replace_battery" and types(det).count("replace_battery") == 2


def test_short_failure_streak_makes_no_events(det):
    feed(det, ok(0), fail(5), fail(10), fail(34), ok(35))
    assert det.seen == []


def test_unreachable_then_recovered(det):
    feed(det, ok(0), fail(5), fail(20), fail(35), fail(40), fail(60))
    assert types(det) == ["ups_unreachable"]
    assert det.seen[0].data["since_ts"] == 5
    feed(det, ok(65))
    assert types(det) == ["ups_unreachable", "ups_recovered"]
    assert "1 min 0 s" in det.seen[1].message
    feed(det, fail(70), fail(120))
    assert types(det).count("ups_unreachable") == 2


def test_refused_streak_also_reported_as_unreachable(det):
    feed(det, fail(0, "refused"), fail(31, "refused"))
    assert types(det) == ["ups_unreachable"]
    assert "refused" in det.seen[0].message


def test_database_failure_still_notifies_callbacks(conn):
    conn.close()
    d = EventDetector(conn)
    seen = []
    d.on_event.append(seen.append)
    d(ok(0, "OB"))
    assert [e.type for e in seen] == ["on_battery"]


def test_callback_exception_is_contained(conn):
    d = EventDetector(conn)
    seen = []
    d.on_event += [lambda e: 1 / 0, seen.append]
    d(ok(0, "OB"))
    assert len(seen) == 1
