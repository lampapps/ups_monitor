import sqlite3

import pytest

from app import db, retention
from app.config import RetentionConfig
from app.retention import RetentionRunner, purge, purge_history, reclaim, rollup, watermark

DAY = 86400
NOW = 1_800_000_000 - 1_800_000_000 % 60 + 30  # 30 s into a minute


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def add_sample(conn, ts, status="OL", charge=100.0, runtime=2400, load=14.0):
    conn.execute(
        "INSERT INTO samples (ts, status, battery_charge, battery_runtime, input_voltage, load_pct) "
        "VALUES (?, ?, ?, ?, 120.0, ?)",
        (ts, status, charge, runtime, load),
    )


def fill(conn, start, end, step=5, **kw):
    with conn:
        for ts in range(start, end, step):
            add_sample(conn, ts, **kw)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- rollup


def test_rollup_computes_minute_aggregates(conn):
    minute = NOW - NOW % 60 - 120
    with conn:
        add_sample(conn, minute + 0, "OL", charge=100, runtime=2400, load=10)
        add_sample(conn, minute + 5, "OB DISCHRG", charge=90, runtime=600, load=20)
        add_sample(conn, minute + 10, "OB DISCHRG", charge=80, runtime=500, load=30)
        add_sample(conn, minute + 60, "OL", charge=100, runtime=2400, load=14)
    assert rollup(conn, NOW) == 2
    first, second = conn.execute("SELECT * FROM samples_1m ORDER BY ts").fetchall()
    assert first["ts"] == minute and first["n"] == 3 and first["ob_count"] == 2
    assert first["battery_charge"] == pytest.approx(90) and first["min_battery_charge"] == 80
    assert first["battery_runtime"] == pytest.approx((2400 + 600 + 500) / 3) and first["min_battery_runtime"] == 500
    assert first["load_pct"] == pytest.approx(20) and first["input_voltage"] == pytest.approx(120)
    assert second["ts"] == minute + 60 and second["n"] == 1 and second["ob_count"] == 0


def test_rollup_skips_the_incomplete_current_minute(conn):
    current_minute = NOW - NOW % 60
    fill(conn, current_minute - 60, current_minute + 25)
    rollup(conn, NOW)
    assert conn.execute("SELECT MAX(ts) FROM samples_1m").fetchone()[0] == current_minute - 60
    assert watermark(conn) == current_minute
    rollup(conn, NOW + 60)
    assert conn.execute("SELECT MAX(ts) FROM samples_1m").fetchone()[0] == current_minute


def test_rollup_is_idempotent_and_incremental(conn):
    fill(conn, NOW - 600, NOW)
    first = rollup(conn, NOW)
    assert first > 0 and rollup(conn, NOW) == 0
    snapshot = [tuple(r) for r in conn.execute("SELECT * FROM samples_1m ORDER BY ts")]
    fill(conn, NOW, NOW + 300)
    assert rollup(conn, NOW + 300) > 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM samples_1m ORDER BY ts")][: len(snapshot)] == snapshot


def test_rollup_with_no_data_does_nothing(conn):
    assert rollup(conn, NOW) == 0 and watermark(conn) is None


def test_rollup_processes_a_long_backlog_in_chunks(conn, monkeypatch):
    monkeypatch.setattr(retention.settings, "ROLLUP_CHUNK_S", 600)
    fill(conn, NOW - 3600, NOW, step=30)
    assert rollup(conn, NOW) >= 59
    assert watermark(conn) == NOW - NOW % 60
    assert count(conn, "samples_1m") >= 59


def test_failed_chunk_does_not_advance_the_watermark(conn, monkeypatch):
    monkeypatch.setattr(retention.settings, "ROLLUP_CHUNK_S", 600)
    fill(conn, NOW - 3600, NOW, step=30)
    real = conn.execute
    calls = {"n": 0}

    class Flaky:
        def __getattr__(self, name):
            return getattr(conn, name)

        def __enter__(self):
            return conn.__enter__()

        def __exit__(self, *a):
            return conn.__exit__(*a)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT OR REPLACE INTO samples_1m"):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise sqlite3.OperationalError("disk I/O error")
            return real(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        rollup(Flaky(), NOW)
    first_minute = (NOW - 3600) - (NOW - 3600) % 60
    assert watermark(conn) == first_minute + 2 * 600  # two committed chunks, the third rolled back
    assert rollup(conn, NOW) > 0 and watermark(conn) == NOW - NOW % 60


# ---------------------------------------------------------------- purge


CFG = RetentionConfig(raw_days=30, rollup_days=365)


def test_purge_deletes_only_expired_rows_after_rollup(conn):
    fill(conn, NOW - 40 * DAY, NOW - 40 * DAY + 600)
    fill(conn, NOW - 10 * DAY, NOW - 10 * DAY + 600)
    rollup(conn, NOW)
    raw, rollups = purge(conn, NOW, CFG)
    assert raw == 120 and rollups == 0
    assert conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0] >= NOW - 30 * DAY
    assert conn.execute("SELECT MIN(ts) FROM samples_1m").fetchone()[0] < NOW - 30 * DAY  # history kept as rollups


def test_purge_never_deletes_raw_rows_that_were_not_rolled_up(conn):
    fill(conn, NOW - 40 * DAY, NOW - 40 * DAY + 600)
    assert purge(conn, NOW, CFG) == (0, 0)  # no rollup has run yet
    assert count(conn, "samples") == 120
    with conn:  # rollup reached only part of the old data
        conn.execute("INSERT INTO monitor_state (key, value, updated_ts) VALUES ('rollup_through', ?, 0)",
                     (str(NOW - 40 * DAY + 300),))
    raw, _ = purge(conn, NOW, CFG)
    assert raw == 60 and count(conn, "samples") == 60


def test_purge_expires_old_rollups(conn):
    with conn:
        for ts in (NOW - 400 * DAY, NOW - 100 * DAY):
            conn.execute("INSERT INTO samples_1m (ts, n, ob_count) VALUES (?, 12, 0)", (ts - ts % 60,))
    assert purge(conn, NOW, CFG) == (0, 1)
    assert count(conn, "samples_1m") == 1


def test_purge_deletes_in_batches(conn, monkeypatch):
    monkeypatch.setattr(retention.settings, "PURGE_BATCH_ROWS", 50)
    fill(conn, NOW - 40 * DAY, NOW - 40 * DAY + 1000)  # 200 rows
    rollup(conn, NOW)
    assert purge(conn, NOW, CFG)[0] == 200 and count(conn, "samples") == 0


def test_old_events_and_finished_shutdown_runs_are_purged(conn):
    old, recent = NOW - 400 * DAY, NOW - 10 * DAY
    with conn:
        for ts in (old, recent):
            conn.execute("INSERT INTO events (ts, type, severity, message) VALUES (?, 't', 'info', 'm')", (ts,))
        for run_id, ts, state in ((1, old, "completed"), (2, old, "running"), (3, recent, "completed")):
            conn.execute("INSERT INTO shutdown_runs (id, started_ts, state, dry_run, reason) VALUES (?, ?, ?, 0, 'r')",
                         (run_id, ts, state))
            conn.execute("INSERT INTO shutdown_steps (run_id, seq, machine, state) VALUES (?, 1, 'a', 'done')", (run_id,))
    assert purge_history(conn, NOW, CFG) == (1, 1)
    assert [r[0] for r in conn.execute("SELECT id FROM shutdown_runs ORDER BY id")] == [2, 3]  # running run kept
    assert [r[0] for r in conn.execute("SELECT run_id FROM shutdown_steps ORDER BY run_id")] == [2, 3]
    assert count(conn, "events") == 1


def test_rollups_must_outlive_raw_data():
    with pytest.raises(ValueError, match="rollup_days"):
        RetentionConfig(raw_days=30, rollup_days=10)


# ---------------------------------------------------------------- disk space


def test_new_databases_use_incremental_auto_vacuum(conn):
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_reclaim_shrinks_the_file_after_deletes(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    c = db.connect(path)
    fill(c, NOW - 40 * DAY, NOW - 40 * DAY + 5 * 30000, step=5)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    big = path.stat().st_size
    with c:
        c.execute("DELETE FROM samples")
    freed = reclaim(c)
    c.close()
    assert freed > 0 and path.stat().st_size < big / 2


def test_legacy_database_is_converted_once(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)  # created without auto_vacuum, as before this milestone
    raw.execute("PRAGMA journal_mode = WAL")
    raw.executescript(db.MIGRATIONS[0] + db.MIGRATIONS[1])
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS)}")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    reclaim(c)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    c.close()


def test_large_legacy_database_is_left_alone(tmp_path, monkeypatch, caplog):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA journal_mode = WAL")
    raw.executescript(db.MIGRATIONS[0] + db.MIGRATIONS[1])
    raw.commit()
    raw.close()
    monkeypatch.setattr(retention.settings, "LEGACY_VACUUM_MAX_BYTES", 1)
    c = db.connect(path)
    reclaim(c)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 0 and "manual VACUUM" in caplog.text
    c.close()


# ---------------------------------------------------------------- runner


def make_runner(tmp_path_db, emit=None, clock=lambda: NOW):
    return RetentionRunner(CFG, lambda: db.connect(tmp_path_db), emit, clock=clock, first_delay_s=0, interval_s=0.01)


def test_run_once_end_to_end(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    c = db.connect(path)
    fill(c, NOW - 40 * DAY, NOW - 40 * DAY + 3000)
    fill(c, NOW - 600, NOW)
    c.close()
    stats = make_runner(path).run_once()
    assert stats.minutes_rolled > 0 and stats.raw_deleted == 600 and stats.did_work
    c = db.connect(path)
    assert count(c, "samples") == 120 and count(c, "samples_1m") > 0
    c.close()
    assert make_runner(path).run_once().raw_deleted == 0


def test_runner_loop_survives_failures_and_reports_once(tmp_path):
    import threading

    path = tmp_path / "ups.db"
    db.init_db(path)
    events, stop, attempts = [], threading.Event(), []

    def factory():
        attempts.append(1)
        if len(attempts) >= 3:
            stop.set()
        raise sqlite3.OperationalError("database is locked")

    runner = RetentionRunner(CFG, factory, lambda *a: events.append(a), first_delay_s=0, interval_s=0.01)
    t = threading.Thread(target=runner.run, args=(stop,))
    t.start()
    t.join(5)
    assert not t.is_alive() and len(attempts) >= 3
    assert [e[1] for e in events] == ["retention_failed"]
    assert events[0][2] == "warning" and "database is locked" in events[0][3]


def test_runner_stop_before_first_run(tmp_path):
    import threading

    stop = threading.Event()
    stop.set()
    called = []
    RetentionRunner(CFG, lambda: called.append(1), first_delay_s=60).run(stop)
    assert called == []
