"""The monitor daemon: python -m app monitor."""

import logging
import os
import signal
import threading
import time

from . import db, settings
from .config import load_config
from .events import EventDetector
from .kuma import KumaPusher
from .notify import Notifier
from .poller import Poller
from .preflight_scheduler import PreflightScheduler
from .retention import RetentionRunner
from .shutdown import ShutdownEngine, ShutdownTrigger, SubprocessRunner

log = logging.getLogger(__name__)


def _background(target, *args, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def run() -> None:
    settings.load_env()
    cfg = load_config()
    db.init_db()
    conn = db.connect()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    # Events can come from several threads, so they get a shared, locked connection.
    event_conn = db.connect(shared=True)
    detector = EventDetector(event_conn)
    poller = Poller(cfg.ups, conn, emit=detector.emit)
    notifier = Notifier(cfg.smtp, os.environ, detector.emit)
    notifier.start()
    detector.on_event.append(notifier.submit)

    runner = SubprocessRunner()
    engine = ShutdownEngine(
        cfg.shutdown, runner, detector.emit, conn_factory=db.connect, before_poweroff=notifier.flush
    )
    engine.recover()
    poller.listeners += [detector, ShutdownTrigger(cfg.shutdown, engine)]

    if cfg.kuma.enabled:
        kuma = KumaPusher(os.environ[cfg.kuma.push_url_env], cfg.kuma.interval_s, cfg.ups.poll_interval_s)
        poller.listeners.append(kuma.observe)
        _background(kuma.run, stop, name="kuma")
    if cfg.shutdown.enabled and (cfg.shutdown.machines or cfg.shutdown.shutdown_self):
        scheduler = PreflightScheduler(
            cfg.shutdown, runner, detector.emit, conn_factory=db.connect, busy=lambda: engine.running
        )
        _background(scheduler.run, stop, name="preflight")
    _background(RetentionRunner(cfg.retention, db.connect, detector.emit).run, stop, name="retention")

    log.info(
        "monitor started (version %s, polling %s:%s, shutdown enabled=%s dry_run=%s, email=%s, kuma=%s)",
        settings.VERSION, cfg.ups.host, cfg.ups.port, cfg.shutdown.enabled, cfg.shutdown.dry_run,
        cfg.smtp.enabled, cfg.kuma.enabled,
    )
    detector.emit(int(time.time()), "monitor_started", "info", f"Monitor started (version {settings.VERSION})", {})
    try:
        poller.run(stop)
    finally:
        stop.set()
        engine.close()
        detector.emit(int(time.time()), "monitor_stopped", "info", "Monitor stopped", {})
        notifier.stop()
        event_conn.close()
        conn.close()
        log.info("monitor stopped")
