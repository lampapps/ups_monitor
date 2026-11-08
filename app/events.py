"""Turn poll results into events (power loss, restore, self-test, UPS unreachable)."""

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, settings
from .poller import PollResult

log = logging.getLogger(__name__)

# Status flags reported once each time they appear: flag -> (event type, severity, text).
_ALERT_FLAGS = {
    "LB": ("low_battery", "critical", "UPS reports low battery"),
    "RB": ("replace_battery", "warning", "UPS reports the battery needs replacing"),
    "OVER": ("overload", "warning", "UPS reports an overload"),
}

_BAD_TEST_WORDS = ("fail", "error", "abort", "warning")


@dataclass(frozen=True)
class Event:
    ts: int
    type: str
    severity: str
    message: str
    data: dict = field(default_factory=dict)


def fmt_local(ts: int) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(settings.DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


class EventDetector:
    """Poller listener. Failed polls only ever produce unreachable/recovered events."""

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        on_event: list[Callable[[Event], None]] | None = None,
        unreachable_after_s: float = settings.UNREACHABLE_AFTER_S,
    ):
        self.conn = conn
        self.on_event = on_event if on_event is not None else []
        self._unreachable_after_s = unreachable_after_s
        self._on_battery = False
        self._battery_since = 0
        self._test_started: int | None = None
        self._active_flags: set[str] = set()
        self._down_since: int | None = None
        self._unreachable_reported = False
        self._emit_lock = threading.Lock()

    def __call__(self, result: PollResult) -> None:
        if result.ok:
            self._handle_ok(result)
        else:
            self._handle_failure(result)

    def _handle_failure(self, result: PollResult) -> None:
        if self._down_since is None:
            self._down_since = result.ts
        down_for = result.ts - self._down_since
        if down_for >= self._unreachable_after_s and not self._unreachable_reported:
            self._unreachable_reported = True
            self.emit(
                result.ts, "ups_unreachable", "warning",
                f"UPS not responding for {fmt_duration(down_for)} ({result.error_kind}: {result.error})",
                {"error_kind": result.error_kind, "since_ts": self._down_since},
            )

    def _handle_ok(self, result: PollResult) -> None:
        ts = result.ts
        flags = set((result.status or "").split())

        if self._down_since is not None:
            if self._unreachable_reported:
                self.emit(
                    ts, "ups_recovered", "info",
                    f"UPS responding again after {fmt_duration(ts - self._down_since)}",
                    {"since_ts": self._down_since},
                )
            self._down_since = None
            self._unreachable_reported = False

        # A self-test shows as "OB CAL" for about 10 s and is not a power loss.
        testing = "CAL" in flags
        if testing and self._test_started is None:
            self._test_started = ts
        elif not testing and self._test_started is not None:
            self._finish_test(result)

        if "OB" in flags and not testing:
            if not self._on_battery:
                self._on_battery = True
                self._battery_since = ts
                self.emit(
                    ts, "on_battery", "warning",
                    f"Power lost at {fmt_local(ts)}: UPS is on battery",
                    {"status": result.status, "battery_charge": result.values.get("battery.charge"),
                     "battery_runtime": result.values.get("battery.runtime")},
                )
        elif "OB" not in flags and self._on_battery:
            self._on_battery = False
            self.emit(
                ts, "power_restored", "info",
                f"Power restored at {fmt_local(ts)} after {fmt_duration(ts - self._battery_since)} on battery",
                {"battery_since_ts": self._battery_since},
            )

        for flag, (type_, severity, text) in _ALERT_FLAGS.items():
            if flag in flags and flag not in self._active_flags:
                self.emit(ts, type_, severity, text, {"status": result.status})
        self._active_flags = flags & _ALERT_FLAGS.keys()

    def _finish_test(self, result: PollResult) -> None:
        started = self._test_started or result.ts
        self._test_started = None
        outcome = result.values.get("ups.test.result", "unknown")
        bad = any(w in outcome.lower() for w in _BAD_TEST_WORDS)
        self.emit(
            result.ts, "self_test", "warning" if bad else "info",
            f"Self-test at {fmt_local(started)}: {outcome} (status showed CAL for about "
            f"{fmt_duration(result.ts - started)})",
            {"started_ts": started, "result": outcome},
        )

    def emit(self, ts: int, type_: str, severity: str, message: str, data: dict) -> None:
        """Thread-safe; also used by the shutdown thread. Callbacks run in the calling thread."""
        log.log(logging.WARNING if severity != "info" else logging.INFO, "event %s: %s", type_, message)
        if self.conn is not None:
            try:
                with self._emit_lock:
                    db.add_event(self.conn, ts, type_, severity, message, data)
            except sqlite3.Error:
                log.exception("could not store event %s", type_)
        event = Event(ts, type_, severity, message, data)
        for callback in self.on_event:
            try:
                callback(event)
            except Exception:
                log.exception("event callback failed")
