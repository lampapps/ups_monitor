import json
import re

import pytest

from app import db, settings
from app.config import Config, ShutdownConfig, UpsConfig
from app.web import create_app

NOW = 1_800_000_000


def sample_rows():
    for i in range(0, 7200, 5):
        ts = NOW - i
        ob = NOW - 600 <= ts <= NOW - 300
        yield (
            ts, "OB DISCHRG" if ob else "OL", 90.0 if ob else 100.0, 13.7, 600 if ob else 2400,
            0.0 if ob else 122.0, 60.0, 120.0, 1.2, 60.0, 90.0, 146.0, 14.0, 25.0,
        )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    conn = db.connect(path)
    with conn:
        conn.executemany(
            "INSERT INTO samples (ts, status, battery_charge, battery_voltage, battery_runtime, input_voltage, "
            "input_frequency, output_voltage, output_current, output_frequency, realpower, power_va, load_pct, "
            "temperature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            list(sample_rows()),
        )
        conn.executemany(
            "INSERT INTO ups_info (key, value, updated_ts) VALUES (?, ?, ?)",
            [("ups.model", "TOWER_1000VA_120V", NOW), ("ups.test.date", "2026-10-28", NOW)],
        )
        conn.execute(
            "INSERT INTO monitor_state (key, value, updated_ts) VALUES ('last_poll', ?, ?)",
            (json.dumps({"ok": True, "error_kind": None, "on_battery": False}), NOW - 2),
        )
        for i, (type_, sev, msg) in enumerate(
            [("monitor_started", "info", "Monitor started"), ("on_battery", "warning", "<script>alert(1)</script>"),
             ("power_restored", "info", "Power back")]
        ):
            conn.execute(
                "INSERT INTO events (ts, type, severity, message, data) VALUES (?, ?, ?, ?, ?)",
                (NOW - 100 + i, type_, sev, msg, json.dumps({"n": i}) if i == 1 else None),
            )
    conn.close()
    return path


@pytest.fixture
def cfg():
    return Config(ups=UpsConfig(host="x"), shutdown=ShutdownConfig(runtime_threshold_minutes=10))


@pytest.fixture
def client(db_path, cfg):
    return create_app(db_path, cfg, clock=lambda: NOW).test_client()


def set_poll(db_path, ts, ok=True, kind=None):
    conn = db.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE monitor_state SET value = ?, updated_ts = ? WHERE key = 'last_poll'",
            (json.dumps({"ok": ok, "error_kind": kind}), ts),
        )
    conn.close()


# ---------------------------------------------------------------- pages and assets


def test_index_is_self_contained_and_has_no_inline_script(client):
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "theme-toggle" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<(?:script|link|img|iframe)\b[^>]*https?://", html)  # plain <a> links are fine
    assert "Content-Security-Policy" in resp.headers and "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["app.js", "app.css", "theme-init.js", "vendor/chart.umd.min.js"])
def test_static_assets_are_served_locally(client, path):
    resp = client.get(f"/static/{path}")
    assert resp.status_code == 200 and len(resp.data) > 100
    resp.close()


@pytest.mark.parametrize("path", ["app.js", "app.css", "theme-init.js"])
def test_our_assets_make_no_external_requests(path):
    text = (settings.STATIC_DIR / path).read_text()
    assert not re.search(r"https?://", text)


def test_app_js_never_writes_html_from_data():
    text = (settings.STATIC_DIR / "app.js").read_text()
    assert "innerHTML" not in text and "insertAdjacentHTML" not in text and "document.write" not in text


# ---------------------------------------------------------------- read only


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/", "/api/current", "/api/history", "/api/events", "/healthz"])
def test_no_write_methods(client, method, path):
    assert getattr(client, method)(path).status_code == 405


def test_api_responses_are_not_cached(client):
    resp = client.get("/api/current")
    assert resp.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------- /api/current


def test_current(client):
    d = client.get("/api/current").get_json()
    assert d["sample"]["status"] == "OL" and d["sample"]["battery_runtime"] == 2400
    assert d["sample_age_s"] == 0
    assert d["info"]["ups.test.date"] == "2026-10-28"
    assert d["monitor"] == {"alive": True, "last_poll_ts": NOW - 2, "ups_ok": True, "error_kind": None}
    assert d["settings"]["timezone"] == "America/New_York" and d["settings"]["stale_after_s"] == 30
    assert d["shutdown"]["dry_run"] is True and d["shutdown"]["runtime_threshold_minutes"] == 10


def test_current_exposes_no_secrets_or_mail_settings(client):
    d = client.get("/api/current").get_json()
    assert set(d) == {"now", "sample", "sample_age_s", "info", "monitor", "settings", "shutdown"}
    assert "smtp" not in json.dumps(d).lower() and "password" not in json.dumps(d).lower()


def test_current_flags_a_stopped_monitor(db_path, cfg):
    set_poll(db_path, NOW - 500)
    client = create_app(db_path, cfg, clock=lambda: NOW).test_client()
    d = client.get("/api/current").get_json()
    assert d["monitor"]["alive"] is False


def test_current_reports_ups_read_failures(db_path, cfg):
    set_poll(db_path, NOW - 1, ok=False, kind="refused")
    d = create_app(db_path, cfg, clock=lambda: NOW).test_client().get("/api/current").get_json()
    assert d["monitor"]["alive"] and d["monitor"]["ups_ok"] is False and d["monitor"]["error_kind"] == "refused"


def test_current_with_empty_database(tmp_path, cfg):
    path = tmp_path / "empty.db"
    db.init_db(path)
    d = create_app(path, cfg, clock=lambda: NOW).test_client().get("/api/current").get_json()
    assert d["sample"] is None and d["sample_age_s"] is None and d["monitor"]["alive"] is False


# ---------------------------------------------------------------- /api/history


def test_history_1h_buckets_and_battery_shading(client):
    d = client.get("/api/history?range=1h").get_json()
    assert d["bucket_s"] == 6 and d["end"] == NOW and d["start"] == NOW - 3600
    assert len(d["t"]) <= 601 and d["t"] == sorted(d["t"])
    n = len(d["t"])
    assert all(len(d[k]) == n for k in ("charge", "runtime_min", "input_v", "load_pct", "temperature", "ob"))
    on_battery_runtime = [r for r, o in zip(d["runtime_min"], d["ob"]) if o]
    assert on_battery_runtime and set(on_battery_runtime) == {10.0}
    assert any(not o for o in d["ob"])
    assert set(d["charge"]) <= {100.0, 90.0, 95.0}


def test_history_24h_is_downsampled(client):
    d = client.get("/api/history").get_json()
    assert d["range"] == "24h" and d["bucket_s"] == 144
    assert 40 <= len(d["t"]) <= 55
    assert any(d["ob"])


def test_history_runtime_uses_minimum_in_bucket(client):
    d = client.get("/api/history?range=24h").get_json()
    assert min(d["runtime_min"]) == 10.0 and max(d["runtime_min"]) == 40.0


def test_history_1y_uses_rollups(db_path, cfg):
    conn = db.connect(db_path)
    with conn:
        for ts, ob in ((NOW - 40 * 86400, 0), (NOW - 40 * 86400 + 600, 3)):
            conn.execute(
                "INSERT INTO samples_1m (ts, n, ob_count, battery_charge, min_battery_runtime, input_voltage) "
                "VALUES (?, 12, ?, 99.0, 1200, 121.0)", (ts - ts % 60, ob),
            )
    conn.close()
    d = create_app(db_path, cfg, clock=lambda: NOW).test_client().get("/api/history?range=1y").get_json()
    assert d["bucket_s"] % 60 == 0 and d["bucket_s"] == 52560
    assert d["runtime_min"] == [20.0] and d["charge"] == [99.0] and d["ob"] == [True]


@pytest.mark.parametrize("bad", ["", "2h", "all", "1h;drop table samples", "' OR 1=1 --"])
def test_history_rejects_unknown_ranges(client, bad):
    resp = client.get("/api/history", query_string={"range": bad})
    assert resp.status_code == 400 and "range must be one of" in resp.get_json()["error"]


# ---------------------------------------------------------------- /api/events


def test_events_newest_first_with_data(client):
    events = client.get("/api/events").get_json()["events"]
    assert [e["type"] for e in events] == ["power_restored", "on_battery", "monitor_started"]
    assert events[1]["data"] == {"n": 1} and events[0]["data"] is None


def test_event_messages_are_returned_as_plain_json_text(client):
    resp = client.get("/api/events")
    assert resp.mimetype == "application/json"
    assert resp.get_json()["events"][1]["message"] == "<script>alert(1)</script>"


def test_events_limit_offset_and_total(client):
    d = client.get("/api/events?limit=2").get_json()
    assert d["total"] == 3 and len(d["events"]) == 2
    rest = client.get("/api/events?limit=2&offset=2").get_json()
    assert rest["total"] == 3 and [e["type"] for e in rest["events"]] == ["monitor_started"]
    assert client.get("/api/events?offset=99").get_json()["events"] == []
    assert len(client.get("/api/events?limit=999999").get_json()["events"]) == 3
    assert len(client.get("/api/events?limit=-5").get_json()["events"]) == 1
    assert client.get("/api/events?offset=-4").get_json()["events"][0]["type"] == "power_restored"


def test_events_pages_cover_everything_exactly_once(db_path, cfg):
    conn = db.connect(db_path)
    with conn:
        for i in range(57):
            conn.execute("INSERT INTO events (ts, type, severity, message) VALUES (?, 'x', 'info', ?)", (NOW + i, f"m{i}"))
    conn.close()
    client = create_app(db_path, cfg, clock=lambda: NOW).test_client()
    seen = []
    for page in range(3):
        d = client.get(f"/api/events?limit=25&offset={page * 25}").get_json()
        assert d["total"] == 60
        seen += [e["id"] for e in d["events"]]
    assert len(seen) == 60 and len(set(seen)) == 60 and seen == sorted(seen, reverse=True)


def test_index_has_event_pager_controls(client):
    html = client.get("/").get_data(as_text=True)
    for element_id in ("ev-first", "ev-prev", "ev-next", "ev-last", "ev-page", "ev-size"):
        assert f'id="{element_id}"' in html
    assert "more-events" not in html


@pytest.mark.parametrize("query", ["limit=abc", "offset=x", "offset=1;DROP TABLE events", "limit=1;DROP TABLE events"])
def test_events_bad_integers(client, query):
    assert client.get(f"/api/events?{query}").status_code == 400


def test_shutdown_runs(db_path, cfg):
    conn = db.connect(db_path)
    with conn:
        conn.execute("INSERT INTO shutdown_runs (id, started_ts, state, dry_run, reason) VALUES (1, 5, 'completed', 1, 'r')")
        conn.execute("INSERT INTO shutdown_steps (run_id, seq, machine, state, detail) VALUES (1, 0, 'iMac', 'done', 'dry run')")
    conn.close()
    runs = create_app(db_path, cfg, clock=lambda: NOW).test_client().get("/api/shutdown-runs").get_json()["runs"]
    assert runs[0]["dry_run"] is True and runs[0]["steps"][0]["machine"] == "iMac"


# ---------------------------------------------------------------- /healthz


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "ups_ok": True, "last_poll_age_s": 2}


def test_healthz_stays_ok_when_only_the_ups_is_unreadable(db_path, cfg):
    set_poll(db_path, NOW - 1, ok=False, kind="unreachable")
    resp = create_app(db_path, cfg, clock=lambda: NOW).test_client().get("/healthz")
    assert resp.status_code == 200 and resp.get_json()["ups_ok"] is False


def test_healthz_fails_when_monitor_stops_polling(db_path, cfg):
    set_poll(db_path, NOW - 31)
    resp = create_app(db_path, cfg, clock=lambda: NOW).test_client().get("/healthz")
    assert resp.status_code == 503 and "not polling" in resp.get_json()["reason"]


def test_healthz_fails_without_any_poll(tmp_path, cfg):
    path = tmp_path / "empty.db"
    db.init_db(path)
    resp = create_app(path, cfg, clock=lambda: NOW).test_client().get("/healthz")
    assert resp.status_code == 503


def test_healthz_and_api_fail_cleanly_without_a_database(tmp_path, cfg):
    client = create_app(tmp_path / "missing.db", cfg, clock=lambda: NOW).test_client()
    assert client.get("/healthz").status_code == 503
    resp = client.get("/api/current")
    assert resp.status_code == 503 and resp.get_json() == {"error": "database unavailable"}
    assert client.get("/").status_code == 200


def test_history_uses_rollups_beyond_the_configured_raw_window(db_path):
    from app.config import RetentionConfig

    cfg = Config(ups=UpsConfig(host="x"), retention=RetentionConfig(raw_days=7, rollup_days=365))
    conn = db.connect(db_path)
    with conn:
        conn.execute("INSERT INTO samples_1m (ts, n, ob_count, battery_charge, min_battery_runtime) VALUES (?, 12, 0, 77.0, 600)",
                     ((NOW - 20 * 86400) // 60 * 60,))
    conn.close()
    client = create_app(db_path, cfg, clock=lambda: NOW).test_client()
    assert client.get("/api/history?range=7d").get_json()["charge"][0] in (90.0, 95.0, 100.0)  # still raw
    d = client.get("/api/history?range=30d").get_json()
    assert d["charge"] == [77.0] and d["bucket_s"] % 60 == 0


def test_info_panel_is_collapsible_and_starts_closed(client):
    html = client.get("/").get_data(as_text=True)
    assert '<details class="panel" id="info-panel">' in html and "<summary>" in html
    assert 'id="ups-info"' in html and 'id="policy-info"' in html and 'id="info-summary"' in html
    js = (settings.STATIC_DIR / "app.js").read_text()
    assert "infoOpen" in js and "info-summary" in js
