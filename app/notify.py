"""Email alerts. Sending happens on a worker thread so SMTP trouble never delays monitoring or shutdown."""

import json
import logging
import queue
import smtplib
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import settings
from .config import SmtpConfig
from .events import Event, fmt_local

log = logging.getLogger(__name__)

Emit = Callable[[int, str, str, str, dict], None]


def event_key(event: Event) -> str:
    """Name under smtp.events for this event."""
    if event.type == "self_test":
        return "self_test_failed" if event.severity != "info" else "self_test_ok"
    if event.type == "shutdown_step":
        return "shutdown_step_failed" if event.severity != "info" else "shutdown_step_ok"
    return event.type


def wants(event: Event, cfg: SmtpConfig) -> bool:
    if event.type in settings.NOTIFY_NEVER_TYPES:
        return False
    return cfg.event_enabled(event_key(event))


def _one_line(text: str) -> str:
    return " ".join(text.split())


def build_message(cfg: SmtpConfig, event: Event, hostname: str) -> EmailMessage:
    msg = EmailMessage()
    # Messages can embed command output, so keep newlines out of headers.
    msg["Subject"] = _one_line(f"[ups_monitor] {event.severity.upper()}: {event.message}")[:200]
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ups_monitor.local")
    lines = [event.message, "", f"Event:  {event.type} ({event.severity})", f"Time:   {fmt_local(event.ts)}",
             f"Sent by: {hostname}"]
    if event.data:
        lines += ["", "Details:", json.dumps(event.data, indent=2, sort_keys=True, default=str)]
    msg.set_content("\n".join(lines))
    return msg


def send_message(cfg: SmtpConfig, env: Mapping[str, str], msg: EmailMessage) -> None:
    """Raises on any failure."""
    username = env.get(cfg.username_env) if cfg.username_env else None
    password = env.get(cfg.password_env) if cfg.password_env else None
    if cfg.security == "ssl":
        client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout_s, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s)
    with client:
        if cfg.security == "starttls":
            client.starttls(context=ssl.create_default_context())
        if username and password:
            client.login(username, password)
        client.send_message(msg)


class Notifier:
    def __init__(
        self,
        cfg: SmtpConfig,
        env: Mapping[str, str],
        emit: Emit,
        send: Callable[[EmailMessage], None] | None = None,
        retry_delays: tuple[float, ...] = settings.NOTIFY_RETRY_DELAYS_S,
        hostname: str | None = None,
    ):
        self.cfg = cfg
        self._emit = emit
        self._send = send or (lambda msg: send_message(cfg, env, msg))
        self._retry_delays = retry_delays
        self._hostname = hostname or socket.gethostname()
        self._queue: queue.Queue[Event | None] = queue.Queue(maxsize=settings.NOTIFY_QUEUE_MAX)
        self._pending = 0
        self._cond = threading.Condition()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.cfg.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
            self._thread.start()

    def submit(self, event: Event) -> None:
        """Non-blocking; safe to call from any thread as an event callback."""
        if not self.cfg.enabled or self._thread is None or not wants(event, self.cfg):
            return
        with self._cond:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                log.error("email queue full, dropping %s", event.type)
                return
            self._pending += 1

    def flush(self, timeout: float = settings.NOTIFY_FLUSH_TIMEOUT_S) -> bool:
        """Wait until queued mail is sent or given up. Returns False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        self._stopping.set()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout)

    def _worker(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._deliver(event)
            finally:
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()

    def _deliver(self, event: Event) -> None:
        msg = build_message(self.cfg, event, self._hostname)
        attempts = len(self._retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                self._send(msg)
                log.info("email sent for %s", event.type)
                return
            except Exception as exc:
                log.warning("email for %s failed (attempt %d/%d): %s", event.type, attempt, attempts, exc)
                if attempt == attempts:
                    self._emit(
                        int(time.time()), "notify_failed", "warning",
                        f"Email for '{event.type}' failed after {attempts} attempts: {exc}",
                        {"event_type": event.type},
                    )
                elif self._stopping.wait(self._retry_delays[attempt - 1]):
                    return
