"""Poll the UPS, store samples. A failed poll is a gap, never a trigger."""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import db, nut, settings
from .config import UpsConfig

log = logging.getLogger(__name__)

_INT_COLUMNS = {"battery_runtime"}


@dataclass
class PollResult:
    ts: int
    ok: bool
    status: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    error_kind: str | None = None
    error: str | None = None

    @property
    def on_battery(self) -> bool:
        return self.status is not None and "OB" in self.status.split()


def _number(value: str | None, as_int: bool) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if as_int else number


def build_sample(ts: int, values: dict[str, str]) -> dict[str, object]:
    sample: dict[str, object] = {"ts": ts}
    for nut_var, column in settings.SAMPLE_FIELDS.items():
        raw = values.get(nut_var)
        sample[column] = raw if column == "status" else _number(raw, column in _INT_COLUMNS)
    return sample


class Poller:
    def __init__(
        self,
        cfg: UpsConfig,
        conn: sqlite3.Connection | None,
        fetch: Callable[..., dict[str, str]] = nut.fetch_vars,
        clock: Callable[[], float] = time.time,
        emit: Callable[[int, str, str, str, dict], None] | None = None,
    ):
        self.cfg = cfg
        self.conn = conn
        self._fetch = fetch
        self._clock = clock
        self._emit = emit
        self._info_cache: dict[str, str] = {}
        self._last_error_kind: str | None = None
        self._on_battery = False
        self.listeners: list[Callable[[PollResult], None]] = []

    def poll_once(self) -> PollResult:
        ts = int(self._clock())
        try:
            values = self._fetch(
                self.cfg.host,
                self.cfg.port,
                self.cfg.name,
                tls=self.cfg.tls,
                timeout=self.cfg.connect_timeout_s,
            )
            if "ups.status" not in values:
                raise nut.NutError(nut.PROTOCOL, "reply had no ups.status")
        except nut.NutError as exc:
            result = PollResult(ts=ts, ok=False, error_kind=exc.kind, error=str(exc))
            self._record_failure(result)
        else:
            result = PollResult(ts=ts, ok=True, status=values["ups.status"], values=values)
            self._on_battery = result.on_battery
            self._last_error_kind = None
            self._store(result)

        self._db(
            self._upsert_state, result.ts,
            json.dumps({"ok": result.ok, "error_kind": result.error_kind, "on_battery": result.on_battery}),
        )
        for listener in self.listeners:
            try:
                listener(result)
            except Exception:
                log.exception("poll listener failed")
        return result

    def next_delay(self) -> float:
        return self.cfg.poll_interval_on_battery_s if self._on_battery else self.cfg.poll_interval_s

    def run(self, stop: threading.Event, wait: Callable[[float], bool] | None = None) -> None:
        wait = wait or stop.wait
        while not stop.is_set():
            started = time.monotonic()
            self.poll_once()
            if wait(max(0.0, self.next_delay() - (time.monotonic() - started))):
                break

    def _record_failure(self, result: PollResult) -> None:
        log.warning("poll failed (%s): %s", result.error_kind, result.error)
        if result.error_kind == nut.REFUSED and self._last_error_kind != nut.REFUSED:
            message = "connection refused (UPS may be at its client limit)"
            if self._emit is not None:
                self._emit(result.ts, "conn_refused", "warning", message, {})
            else:
                self._db(db.add_event, result.ts, "conn_refused", "warning", message)
        self._last_error_kind = result.error_kind

    def _store(self, result: PollResult) -> None:
        sample = build_sample(result.ts, result.values)
        columns = ", ".join(sample)
        marks = ", ".join(f":{c}" for c in sample)
        self._db(self._insert_sample, f"INSERT OR REPLACE INTO samples ({columns}) VALUES ({marks})", sample)

        changed = {
            k: result.values[k]
            for k in settings.INFO_FIELDS
            if k in result.values and self._info_cache.get(k) != result.values[k]
        }
        if changed and self._db(self._upsert_info, result.ts, changed):
            self._info_cache.update(changed)

    def _db(self, fn: Callable, *args):
        # The monitor and shutdown logic must keep working when the database fails.
        if self.conn is None:
            return None
        try:
            return fn(self.conn, *args)
        except sqlite3.Error:
            log.exception("database write failed")
            return None

    @staticmethod
    def _upsert_state(conn: sqlite3.Connection, ts: int, value: str) -> bool:
        with conn:
            conn.execute(
                "INSERT INTO monitor_state (key, value, updated_ts) VALUES ('last_poll', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                (value, ts),
            )
        return True

    @staticmethod
    def _insert_sample(conn: sqlite3.Connection, sql: str, sample: dict) -> bool:
        with conn:
            conn.execute(sql, sample)
        return True

    @staticmethod
    def _upsert_info(conn: sqlite3.Connection, ts: int, changed: dict[str, str]) -> bool:
        with conn:
            conn.executemany(
                "INSERT INTO ups_info (key, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                [(k, v, ts) for k, v in changed.items()],
            )
        return True
