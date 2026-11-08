"""Web UI. The database is opened read-only; the only action is the optional "Check now" request."""

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, g, jsonify, render_template, request, url_for

from .. import db, settings
from ..config import Config, load_config
from . import queries

log = logging.getLogger(__name__)

_CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def create_app(
    db_path: Path = settings.DB_PATH,
    cfg: Config | None = None,
    clock: Callable[[], float] = time.time,
    request_path: Path = settings.CHECK_REQUEST_PATH,
) -> Flask:
    # The web process may not have the .env secrets, and it never needs them.
    cfg = cfg or load_config(check_env=False)
    stale_after_s = max(3 * cfg.ups.poll_interval_s, settings.HEALTH_STALE_MIN_S)

    app = Flask(__name__, static_folder=str(settings.STATIC_DIR), static_url_path="/static")
    app.json.sort_keys = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # revalidate (cheap 304) so upgrades show immediately

    @app.context_processor
    def asset_helpers():
        def asset(name: str) -> str:
            try:
                version = int((settings.STATIC_DIR / name).stat().st_mtime)
            except OSError:
                version = 0
            return url_for("static", filename=name, v=version)

        return {"asset": asset}

    def conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect_readonly(db_path)
            g.db.execute("PRAGMA query_only = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_exc) -> None:
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = _CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith(("/api/", "/healthz")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(sqlite3.Error)
    def database_error(exc):
        log.warning("database error: %s", exc)
        return jsonify(error="database unavailable"), 503

    def int_arg(name: str, default: int, low: int, high: int) -> int:
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            abort(400, f"{name} must be an integer")
        return min(max(value, low), high)

    @app.get("/")
    def index():
        return render_template("index.html", version=settings.VERSION)

    @app.get("/healthz")
    def healthz():
        now = int(clock())
        try:
            poll = queries.last_poll(conn())
        except sqlite3.Error:
            return jsonify(status="error", reason="database unavailable"), 503
        if poll is None:
            return jsonify(status="error", reason="monitor has not recorded a poll"), 503
        age = now - poll["ts"]
        if age > stale_after_s:
            return jsonify(status="error", reason=f"monitor not polling (last poll {age} s ago)"), 503
        return jsonify(status="ok", ups_ok=poll["ok"], last_poll_age_s=age)

    @app.get("/api/current")
    def api_current():
        now = int(clock())
        sample = queries.latest_sample(conn())
        poll = queries.last_poll(conn())
        monitor_alive = poll is not None and now - poll["ts"] <= stale_after_s
        s = cfg.shutdown
        return jsonify(
            now=now,
            sample=sample,
            sample_age_s=None if sample is None else now - sample["ts"],
            info=queries.ups_info(conn()),
            monitor={
                "alive": monitor_alive,
                "last_poll_ts": poll["ts"] if poll else None,
                "ups_ok": poll["ok"] if poll else None,
                "error_kind": poll["error_kind"] if poll else None,
            },
            settings={
                "timezone": settings.DISPLAY_TIMEZONE,
                "poll_interval_s": cfg.ups.poll_interval_s,
                "stale_after_s": stale_after_s,
                "version": settings.VERSION,
            },
            shutdown={
                "enabled": s.enabled,
                "dry_run": s.dry_run,
                "runtime_threshold_minutes": s.runtime_threshold_minutes,
                "consecutive_polls": s.consecutive_polls,
                "shutdown_self": s.shutdown_self,
                "machines": [m.name for m in s.machines],
            },
        )

    def monitor_alive(now: int) -> bool:
        poll = queries.last_poll(conn())
        return poll is not None and now - poll["ts"] <= stale_after_s

    @app.get("/api/readiness")
    def api_readiness():
        now = int(clock())
        view = queries.readiness_view(conn(), cfg.shutdown, now, request_path, monitor_alive(now))
        view["allow_check_now"] = cfg.web.allow_check_now
        view["cooldown_s"] = settings.CHECK_COOLDOWN_S
        return jsonify(view)

    @app.post("/api/check-now")
    def api_check_now():
        if not cfg.web.allow_check_now:
            abort(404)
        # A custom header and a same-origin check keep other web pages from triggering this.
        if request.headers.get("X-Requested-With") != "ups-monitor-ui":
            return jsonify(error="missing X-Requested-With header"), 403
        origin = request.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc != request.host:
            return jsonify(error="cross-origin request refused"), 403

        now = int(clock())
        alive = monitor_alive(now)
        if not alive:
            return jsonify(error="the monitor is not running"), 503
        view = queries.readiness_view(conn(), cfg.shutdown, now, request_path, alive)
        if view["state"] != "idle":
            return jsonify(error="a check is already pending or running", state=view["state"]), 409
        finished = view["finished_ts"]
        if finished and now - finished < settings.CHECK_COOLDOWN_S:
            wait = settings.CHECK_COOLDOWN_S - (now - finished)
            return jsonify(error="a check finished moments ago", retry_after_s=wait), 429
        try:
            os.close(os.open(request_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return jsonify(error="a check is already pending or running", state="pending"), 409
        except OSError:
            log.exception("cannot create the check-now request file")
            return jsonify(error="could not create the request"), 500
        return jsonify(state="pending"), 202

    @app.get("/api/history")
    def api_history():
        range_key = request.args.get("range", "24h")
        if range_key not in queries.RANGES:
            abort(400, f"range must be one of {', '.join(queries.RANGES)}")
        return jsonify(queries.history(conn(), range_key, int(clock()), cfg.retention.raw_days * 86400))

    @app.get("/api/events")
    def api_events():
        limit = int_arg("limit", 50, 1, settings.WEB_EVENTS_MAX)
        offset = int_arg("offset", 0, 0, 2**31)
        return jsonify(total=queries.events_total(conn()), events=queries.events(conn(), limit, offset))

    @app.get("/api/shutdown-runs")
    def api_shutdown_runs():
        return jsonify(runs=queries.shutdown_runs(conn(), int_arg("limit", 10, 1, 50)))

    @app.errorhandler(400)
    def bad_request(exc):
        return jsonify(error=exc.description), 400

    return app
