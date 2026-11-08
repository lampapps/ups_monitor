"""Runs the shutdown-readiness check on a schedule and on request, stores the results and raises alerts."""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import readiness, settings
from .config import ShutdownConfig
from .shutdown import Runner, TargetCheck, preflight

log = logging.getLogger(__name__)

Emit = Callable[[int, str, str, str, dict], None]


class PreflightScheduler:
    def __init__(
        self,
        cfg: ShutdownConfig,
        runner: Runner,
        emit: Emit,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        busy: Callable[[], bool] = lambda: False,
        first_delay_s: float = settings.PREFLIGHT_FIRST_DELAY_S,
        request_path: Path = settings.CHECK_REQUEST_PATH,
        poll_s: float = settings.SCHEDULER_POLL_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self._runner = runner
        self._emit = emit
        self._conn_factory = conn_factory
        self._busy = busy
        self._first_delay_s = first_delay_s
        self._request_path = Path(request_path)
        self._poll_s = poll_s
        self._clock = clock
        self._monotonic = monotonic
        self._failing = False

    # -- database helpers: a database problem must never stop the check itself

    def _open(self) -> sqlite3.Connection | None:
        if self._conn_factory is None:
            return None
        try:
            return self._conn_factory()
        except sqlite3.Error:
            log.exception("readiness: cannot open the database")
            return None

    @staticmethod
    def _safe(conn: sqlite3.Connection | None, fn: Callable, *args, **kwargs) -> None:
        if conn is None:
            return
        try:
            fn(conn, *args, **kwargs)
        except sqlite3.Error:
            log.exception("readiness: database write failed")

    # -- one check

    def run_once(self, trigger: str = "daily") -> bool:
        """Returns False when skipped because a shutdown sequence is running."""
        started = int(self._clock())
        conn = self._open()
        checks: list[TargetCheck] | None = None
        try:
            if self._busy():  # never probe targets while a shutdown sequence is running
                self._safe(conn, readiness.set_run, started, "idle", trigger, finished_ts=started,
                           note="skipped: a shutdown sequence is running")
                return False
            self._safe(conn, readiness.set_run, started, "running", trigger, started_ts=started)
            try:
                checks = preflight(self.cfg, self._runner)
                self._safe(conn, readiness.save, checks, int(self._clock()), trigger)
            finally:
                finished = int(self._clock())
                self._safe(conn, readiness.set_run, finished, "idle", trigger, started_ts=started,
                           finished_ts=finished, note=None if checks is not None else "the check did not complete")
        finally:
            if conn is not None:
                conn.close()
        self._report(checks, trigger)
        return True

    def _report(self, checks: list[TargetCheck], trigger: str) -> None:
        failed = [c for c in checks if not c.ok]
        now = int(self._clock())
        if failed:
            already_failing, self._failing = self._failing, True
            if trigger in ("manual", "cli") and already_failing:
                log.warning("readiness check still failing for %d target(s)", len(failed))
                return  # repeated button presses must not repeat the alert; the daily run still does
            details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
            self._emit(
                now, "preflight_failed", "warning",
                f"Shutdown readiness check failed for {len(failed)} of {len(checks)} targets: {details}",
                {"failed": [c.name for c in failed], "trigger": trigger},
            )
        elif self._failing:
            self._failing = False
            self._emit(now, "preflight_ok", "info", "Shutdown readiness check passes again on all targets",
                       {"trigger": trigger})
        else:
            log.info("shutdown readiness OK (%d targets, %s)", len(checks), trigger)

    # -- loop

    def _consume_request(self) -> bool:
        try:
            self._request_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            log.exception("readiness: cannot remove the check-now request file")
            return False

    def _startup(self) -> None:
        self._consume_request()  # a request left behind by an earlier run is stale
        conn = self._open()
        if conn is None:
            return
        try:
            now = int(self._clock())
            self._safe(conn, readiness.prune, [m.name for m in self.cfg.machines] + (
                ["this host"] if self.cfg.shutdown_self else []))
            if readiness.get_run(conn)["state"] == "running":
                self._safe(conn, readiness.set_run, now, "idle", finished_ts=now, note="monitor restarted")
        except sqlite3.Error:
            log.exception("readiness: startup cleanup failed")
        finally:
            conn.close()

    def run(self, stop: threading.Event) -> None:
        self._startup()
        interval = self.cfg.preflight_interval_hours * 3600
        due = self._monotonic() + self._first_delay_s if interval > 0 else None
        first = True
        while not stop.wait(self._poll_s):
            trigger = None
            if self._consume_request():
                trigger = "manual"
            elif due is not None and self._monotonic() >= due:
                trigger = "startup" if first else "daily"
                first = False
                due = self._monotonic() + interval
            if trigger is None:
                continue
            try:
                self.run_once(trigger)
            except Exception:
                log.exception("readiness check failed")
