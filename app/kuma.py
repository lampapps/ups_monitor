"""Uptime Kuma push heartbeat. Kuma marks the app down when pushes stop, or when we push status=down."""

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import settings
from .events import fmt_duration
from .poller import PollResult

log = logging.getLogger(__name__)


class KumaPusher:
    def __init__(
        self,
        push_url: str,
        interval_s: float,
        poll_interval_s: float,
        unreachable_after_s: float = settings.UNREACHABLE_AFTER_S,
        clock: Callable[[], float] = time.time,
        opener: Callable[[str, float], None] | None = None,
    ):
        parsed = urllib.parse.urlsplit(push_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Kuma push URL must be an http(s) URL")
        self._base = parsed
        self.interval_s = interval_s
        self._stale_after_s = max(3 * poll_interval_s, settings.KUMA_STALE_MIN_S)
        self._unreachable_after_s = unreachable_after_s
        self._clock = clock
        self._open = opener or self._http_get
        self._last: PollResult | None = None
        self._fail_since: int | None = None
        self._failing = False

    def observe(self, result: PollResult) -> None:
        """Poller listener."""
        if result.ok:
            self._fail_since = None
        elif self._fail_since is None:
            self._fail_since = result.ts
        self._last = result

    def status(self) -> tuple[str, str] | None:
        """(status, message) for Kuma, or None before the first poll."""
        last = self._last
        if last is None:
            return None
        now = self._clock()
        if now - last.ts > self._stale_after_s:
            return "down", f"no poll result for {fmt_duration(int(now - last.ts))}"
        if not last.ok:
            since = self._fail_since if self._fail_since is not None else last.ts
            if last.ts - since >= self._unreachable_after_s:
                return "down", f"UPS unreachable ({last.error_kind}): {last.error}"
            return "up", "UPS poll failing, within grace period"
        charge = last.values.get("battery.charge", "?")
        minutes = ""
        try:
            minutes = f", runtime {float(last.values['battery.runtime']) / 60:.0f} min"
        except (KeyError, ValueError):
            pass
        return "up", f"UPS {last.status}, charge {charge}%{minutes}"

    def build_url(self, status: str, msg: str) -> str:
        query = urllib.parse.urlencode({"status": status, "msg": msg})
        return urllib.parse.urlunsplit(self._base._replace(query=query))

    def push_once(self) -> bool | None:
        state = self.status()
        if state is None:
            return None
        try:
            self._open(self.build_url(*state), settings.KUMA_TIMEOUT_S)
        except Exception as exc:
            if not self._failing:
                # Never log the URL: it contains the push token.
                log.warning("Kuma push failed: %s", type(exc).__name__)
            self._failing = True
            return False
        self._failing = False
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.interval_s):
            self.push_once()

    @staticmethod
    def _http_get(url: str, timeout: float) -> None:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 scheme checked in __init__
            resp.read(256)
