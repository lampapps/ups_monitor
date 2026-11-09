"""Command line helpers: python -m app <initdb|config|poll|check|drill|testmail|testkuma|retention|monitor|web>."""

import argparse
import logging
import os
import socket
import sqlite3
import sys
import time

from . import config, db, kuma, nut, readiness, settings, shutdown
from .events import Event
from .notify import build_message, send_message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument(
        "command",
        choices=["initdb", "config", "poll", "check", "drill", "testmail", "testkuma", "retention", "monitor", "web"],
        help="poll: one read of the UPS, nothing stored. check: verify ping/ssh/sudo on every shutdown target, "
        "powers nothing off. testmail/testkuma: send one test message to verify the setup. "
        "retention: run the rollup/purge once now. "
        "drill: rehearse the shutdown sequence in forced dry-run mode (sends the real emails)",
    )
    parser.add_argument(
        "--trust-new-hosts",
        action="store_true",
        help="check only: pin host keys of machines not yet in keys/known_hosts (use once, on a trusted LAN)",
    )
    parser.add_argument("--dev", action="store_true", help="web only: Flask dev server instead of gunicorn")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.load_env()
    if args.command == "initdb":
        version = db.init_db()
        print(f"{settings.DB_PATH}: schema version {version}")
        return 0
    if args.command == "web":
        from .web.server import serve

        serve(dev=args.dev)
        return 0

    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.command == "config":
        print(f"{settings.CONFIG_PATH}: OK ({len(cfg.shutdown.machines)} shutdown machines, dry_run={cfg.shutdown.dry_run})")
    elif args.command == "poll":
        try:
            values = nut.fetch_vars(
                cfg.ups.host, cfg.ups.port, cfg.ups.name, tls=cfg.ups.tls, timeout=cfg.ups.connect_timeout_s
            )
        except nut.NutError as exc:
            print(f"{exc.kind}: {exc}", file=sys.stderr)
            return 1
        for key in sorted(values):
            print(f"{key} = {values[key]}")
    elif args.command == "check":
        results = shutdown.preflight(cfg.shutdown, shutdown.SubprocessRunner(), accept_new=args.trust_new_hosts)
        for r in results:
            print(f"{'OK  ' if r.ok else 'FAIL'} {r.name}: {r.detail}")
        try:  # so the dashboard shows what was just verified
            db.init_db()
            conn = db.connect()
            try:
                readiness.save(conn, results, int(time.time()), "cli")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"note: results not stored for the dashboard ({exc})", file=sys.stderr)
        return 0 if all(r.ok for r in results) else 1
    elif args.command == "drill":
        from .drill import run_drill

        for machine, state, detail in run_drill(cfg, os.environ):
            print(f"{state:8} {machine}: {detail}")
    elif args.command == "testmail":
        if not cfg.smtp.enabled:
            print("smtp.enabled is false in config.yaml", file=sys.stderr)
            return 1
        event = Event(int(time.time()), "test", "info", "Test message from ups_monitor")
        try:
            send_message(cfg.smtp, os.environ, build_message(cfg.smtp, event, socket.gethostname()))
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"OK: sent to {', '.join(cfg.smtp.recipients)}")
    elif args.command == "retention":
        from .retention import RetentionRunner

        stats = RetentionRunner(cfg.retention, db.connect).run_once()
        print(
            f"rolled up {stats.minutes_rolled} minutes, deleted {stats.raw_deleted} raw and "
            f"{stats.rollups_deleted} rollup rows, {stats.events_deleted} events and "
            f"{stats.shutdown_runs_deleted} shutdown runs, freed {stats.pages_freed} pages"
        )
    elif args.command == "testkuma":
        if not cfg.kuma.enabled:
            print("kuma.enabled is false in config.yaml", file=sys.stderr)
            return 1
        pusher = kuma.KumaPusher(os.environ[cfg.kuma.push_url_env], cfg.kuma.interval_s, cfg.ups.poll_interval_s)
        try:
            pusher._open(pusher.build_url("up", "test from ups_monitor"), settings.KUMA_TIMEOUT_S)
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}", file=sys.stderr)
            return 1
        print("OK: push sent")
    else:
        from . import monitor

        monitor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
