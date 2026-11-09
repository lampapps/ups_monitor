"""Hourly rollup of raw samples into 1-minute averages, purge by age, reclaim disk space.

Raw rows are only ever deleted after they have been rolled up.
"""

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import settings
from .config import RetentionConfig

log = logging.getLogger(__name__)

Emit = Callable[[int, str, str, str, dict], None]

_OB = "instr(' ' || status || ' ', ' OB ') > 0"

_ROLLUP_SQL = f"""
INSERT OR REPLACE INTO samples_1m (
    ts, n, ob_count, battery_charge, min_battery_charge, battery_voltage, battery_runtime,
    min_battery_runtime, input_voltage, input_frequency, output_voltage, output_current,
    output_frequency, realpower, power_va, load_pct, temperature)
SELECT (ts / 60) * 60, COUNT(*), SUM({_OB}), AVG(battery_charge), MIN(battery_charge),
       AVG(battery_voltage), AVG(battery_runtime), MIN(battery_runtime), AVG(input_voltage),
       AVG(input_frequency), AVG(output_voltage), AVG(output_current), AVG(output_frequency),
       AVG(realpower), AVG(power_va), AVG(load_pct), AVG(temperature)
FROM samples WHERE ts >= ? AND ts < ? GROUP BY ts / 60
"""

_WATERMARK_KEY = "rollup_through"


@dataclass
class RetentionStats:
    minutes_rolled: int = 0
    raw_deleted: int = 0
    rollups_deleted: int = 0
    events_deleted: int = 0
    shutdown_runs_deleted: int = 0
    pages_freed: int = 0

    @property
    def did_work(self) -> bool:
        return any((self.minutes_rolled, self.raw_deleted, self.rollups_deleted, self.events_deleted,
                    self.shutdown_runs_deleted, self.pages_freed))


def watermark(conn: sqlite3.Connection) -> int | None:
    """Start of the first minute not yet rolled up, or None before the first rollup."""
    row = conn.execute("SELECT value FROM monitor_state WHERE key = ?", (_WATERMARK_KEY,)).fetchone()
    try:
        return int(row[0]) if row else None
    except ValueError:
        return None


def rollup(conn: sqlite3.Connection, now: int) -> int:
    """Roll up every complete minute since the last run. Returns the number of minute rows written."""
    complete_until = now - now % 60
    start = watermark(conn)
    if start is None:
        first = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        if first is None:
            return 0
        start = first - first % 60

    written = 0
    while start < complete_until:
        end = min(start + settings.ROLLUP_CHUNK_S, complete_until)
        with conn:
            written += conn.execute(_ROLLUP_SQL, (start, end)).rowcount
            conn.execute(
                "INSERT INTO monitor_state (key, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                (_WATERMARK_KEY, str(end), now),
            )
        start = end
    return written


def _delete_older(conn: sqlite3.Connection, table: str, cutoff: int) -> int:
    total = 0
    while True:
        with conn:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE ts IN (SELECT ts FROM {table} WHERE ts < ? LIMIT ?)",
                (cutoff, settings.PURGE_BATCH_ROWS),
            )
        total += cur.rowcount
        if cur.rowcount < settings.PURGE_BATCH_ROWS:
            return total


def purge(conn: sqlite3.Connection, now: int, cfg: RetentionConfig) -> tuple[int, int]:
    """Delete expired rows. Returns (raw rows, rollup rows) deleted."""
    marker = watermark(conn)
    raw = 0
    if marker is not None:
        # Never delete raw data that has not been rolled up yet, e.g. after a failed rollup.
        raw = _delete_older(conn, "samples", min(now - cfg.raw_days * 86400, marker))
    rollups = _delete_older(conn, "samples_1m", now - cfg.rollup_days * 86400)
    return raw, rollups


def purge_history(conn: sqlite3.Connection, now: int, cfg: RetentionConfig) -> tuple[int, int]:
    """Delete expired events and finished shutdown runs with their steps. Returns (events, runs) deleted."""
    with conn:
        events = conn.execute("DELETE FROM events WHERE ts < ?", (now - cfg.events_days * 86400,)).rowcount
        cutoff = now - cfg.shutdown_days * 86400
        expired = "SELECT id FROM shutdown_runs WHERE started_ts < ? AND state != 'running'"
        conn.execute(f"DELETE FROM shutdown_steps WHERE run_id IN ({expired})", (cutoff,))
        runs = conn.execute(f"DELETE FROM shutdown_runs WHERE id IN ({expired})", (cutoff,)).rowcount
    return events, runs


def _freelist(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA freelist_count").fetchone()[0]


def reclaim(conn: sqlite3.Connection) -> int:
    """Give free pages back to the filesystem. Returns pages freed."""
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0:
        _convert_legacy(conn)
    before = _freelist(conn)
    for _ in range(settings.VACUUM_MAX_STEPS):
        if _freelist(conn) == 0:
            break
        conn.execute(f"PRAGMA incremental_vacuum({settings.VACUUM_PAGES_PER_STEP})").fetchall()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return before - _freelist(conn)


def _convert_legacy(conn: sqlite3.Connection) -> None:
    """One-time VACUUM so an older database can use incremental vacuum; skipped when it would block too long."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path or os.path.getsize(path) > settings.LEGACY_VACUUM_MAX_BYTES:
        log.warning("database predates auto_vacuum and is large; run a manual VACUUM to enable space reclaim")
        return
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("VACUUM")
    log.info("converted database to incremental auto_vacuum")


class RetentionRunner:
    def __init__(
        self,
        cfg: RetentionConfig,
        conn_factory: Callable[[], sqlite3.Connection],
        emit: Emit | None = None,
        clock: Callable[[], float] = time.time,
        first_delay_s: float = settings.RETENTION_FIRST_DELAY_S,
        interval_s: float = settings.RETENTION_INTERVAL_S,
    ):
        self.cfg = cfg
        self._conn_factory = conn_factory
        self._emit = emit
        self._clock = clock
        self._first_delay_s = first_delay_s
        self._interval_s = interval_s
        self._failing = False

    def run_once(self) -> RetentionStats:
        now = int(self._clock())
        conn = self._conn_factory()
        try:
            stats = RetentionStats(minutes_rolled=rollup(conn, now))
            stats.raw_deleted, stats.rollups_deleted = purge(conn, now, self.cfg)
            stats.events_deleted, stats.shutdown_runs_deleted = purge_history(conn, now, self.cfg)
            stats.pages_freed = reclaim(conn)
        finally:
            conn.close()
        if stats.did_work:
            log.info(
                "retention: rolled up %d minutes, deleted %d raw and %d rollup rows, %d events and %d shutdown runs, "
                "freed %d pages",
                stats.minutes_rolled, stats.raw_deleted, stats.rollups_deleted, stats.events_deleted,
                stats.shutdown_runs_deleted, stats.pages_freed,
            )
        return stats

    def run(self, stop: threading.Event) -> None:
        if stop.wait(self._first_delay_s):
            return
        while True:
            self._guarded_run()
            if stop.wait(self._interval_s):
                return

    def _guarded_run(self) -> None:
        try:
            self.run_once()
            self._failing = False
        except Exception as exc:
            log.exception("retention run failed")
            if not self._failing and self._emit is not None:
                self._emit(
                    int(self._clock()), "retention_failed", "warning",
                    f"Data retention failed ({type(exc).__name__}: {exc}); the database may grow until it succeeds",
                    {},
                )
            self._failing = True
