import json
import time

import pytest

from app import db, readiness, settings
from app.config import Config, MachineConfig, ShutdownConfig, UpsConfig, WebConfig
from app.shutdown import StepResult, TargetCheck
from app.web import create_app

NOW = 1_800_000_000
OK = StepResult(True, "fine")


def make_cfg(allow=True, hours=24.0):
    return Config(
        ups=UpsConfig(host="x"),
        shutdown=ShutdownConfig(
            preflight_interval_hours=hours,
            machines=[MachineConfig(name="iMac", host="10.10.2.20", user="u"),
                      MachineConfig(name="pi4-2", host="10.10.1.3", user="u")],
        ),
        web=WebConfig(allow_check_now=allow),
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO monitor_state (key, value, updated_ts) VALUES ('last_poll', ?, ?)",
                     (json.dumps({"ok": True}), NOW - 1))
    conn.close()
    return path


@pytest.fixture
def req(tmp_path):
    return tmp_path / "check-now.request"


def client_for(db_path, req, cfg=None, now=NOW):
    return create_app(db_path, cfg or make_cfg(), clock=lambda: now, request_path=req).test_client()


def save(db_path, checks, ts=NOW - 100, trigger="daily"):
    conn = db.connect(db_path)
    try:
        readiness.save(conn, checks, ts, trigger)
    finally:
        conn.close()


def set_run(db_path, state, **kw):
    conn = db.connect(db_path)
    try:
        readiness.set_run(conn, NOW, state, "manual", **kw)
    finally:
        conn.close()


HEADERS = {"X-Requested-With": "ups-monitor-ui"}


# ---------------------------------------------------------------- GET /api/readiness


def test_before_any_check_lists_configured_machines_as_unverified(db_path, req):
    d = client_for(db_path, req).get("/api/readiness").get_json()
    assert [(t["position"], t["name"], t["overall"]) for t in d["targets"]] == [
        (1, "iMac", "unverified"), (2, "pi4-2", "unverified"), (3, "this host", "unverified")]
    assert d["state"] == "idle" and d["checked_ts"] is None and d["monitor_alive"] is True
    assert d["allow_check_now"] is True and d["cooldown_s"] == settings.CHECK_COOLDOWN_S
    assert all(set(t["steps"]) == {"ping", "ssh", "sudo", "dry"} and not any(t["steps"].values()) for t in d["targets"])


def test_results_are_returned_in_configured_order(db_path, req):
    save(db_path, [
        TargetCheck("this host", 3, None, None, OK, OK),
        TargetCheck("pi4-2", 2, OK, OK, OK, StepResult(None, "not enabled: re-run setup-target.sh")),
        TargetCheck("iMac", 1, OK, OK, OK, OK, "imac-1"),
    ], trigger="manual")
    d = client_for(db_path, req).get("/api/readiness").get_json()
    imac, pi, me = d["targets"]
    assert (imac["name"], imac["overall"], imac["remote_hostname"], imac["checked_ts"]) == ("iMac", "ready", "imac-1", NOW - 100)
    assert imac["steps"]["dry"] == {"ok": True, "detail": "fine"}
    assert pi["overall"] == "partial" and pi["steps"]["dry"]["ok"] is None and "setup-target.sh" in pi["steps"]["dry"]["detail"]
    assert me["steps"]["ping"] is None and me["steps"]["ssh"] is None
    assert d["checked_ts"] == NOW - 100 and d["trigger"] is None  # trigger of the run state, not of the rows


def test_failures_show_as_attention_with_the_reason(db_path, req):
    bad = StepResult(False, "sudo check failed (exit 1): sudo: a password is required")
    save(db_path, [TargetCheck("iMac", 1, OK, OK, bad, OK)])
    t = client_for(db_path, req).get("/api/readiness").get_json()["targets"][0]
    assert t["overall"] == "attention" and t["steps"]["sudo"]["ok"] is False
    assert "password is required" in t["steps"]["sudo"]["detail"]


def test_old_verification_becomes_unverified_but_failures_stay_failures(db_path, req):
    old = NOW - 3 * 24 * 3600
    save(db_path, [TargetCheck("iMac", 1, OK, OK, OK, OK), TargetCheck("pi4-2", 2, OK, StepResult(False, "x"), None, None)], ts=old)
    targets = client_for(db_path, req).get("/api/readiness").get_json()["targets"]
    assert targets[0]["overall"] == "unverified" and targets[0]["stale"] is True
    assert targets[1]["overall"] == "attention"


def test_staleness_is_off_when_the_schedule_is_off(db_path, req):
    save(db_path, [TargetCheck("iMac", 1, OK, OK, OK, OK)], ts=NOW - 400 * 24 * 3600)
    t = client_for(db_path, req, make_cfg(hours=0)).get("/api/readiness").get_json()["targets"][0]
    assert t["overall"] == "ready" and t["stale"] is False


def test_state_pending_running_idle(db_path, req):
    c = client_for(db_path, req)
    assert c.get("/api/readiness").get_json()["state"] == "idle"
    req.write_text("")
    assert c.get("/api/readiness").get_json()["state"] == "pending"
    req.unlink()
    set_run(db_path, "running", started_ts=NOW - 3)
    assert c.get("/api/readiness").get_json()["state"] == "running"


def test_a_dead_monitor_never_looks_like_a_running_check(db_path, req):
    set_run(db_path, "running", started_ts=NOW - 3)
    d = client_for(db_path, req, now=NOW + 3600).get("/api/readiness").get_json()
    assert d["state"] == "idle" and d["monitor_alive"] is False


def test_no_addresses_in_the_api(db_path, req):
    save(db_path, [TargetCheck("iMac", 1, OK, OK, OK, OK, "imac-1")])
    text = client_for(db_path, req).get("/api/readiness").get_data(as_text=True)
    assert "10.10." not in text and '"host"' not in text and "upsshutdown" not in text


def test_panel_is_hidden_data_when_nothing_is_configured(db_path, req):
    cfg = Config(ups=UpsConfig(host="x"), shutdown=ShutdownConfig(shutdown_self=False))
    assert client_for(db_path, req, cfg).get("/api/readiness").get_json()["targets"] == []


# ---------------------------------------------------------------- POST /api/check-now


def test_check_now_creates_the_request_file(db_path, req):
    resp = client_for(db_path, req).post("/api/check-now", headers=HEADERS)
    assert resp.status_code == 202 and resp.get_json() == {"state": "pending"}
    assert req.exists() and req.stat().st_mode & 0o777 == 0o600


def test_check_now_needs_the_custom_header(db_path, req):
    c = client_for(db_path, req)
    assert c.post("/api/check-now").status_code == 403
    assert c.post("/api/check-now", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 403
    assert not req.exists()


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:9999"])
def test_check_now_refuses_other_origins(db_path, req, origin):
    resp = client_for(db_path, req).post("/api/check-now", headers={**HEADERS, "Origin": origin})
    assert resp.status_code == 403 and not req.exists()


def test_check_now_accepts_its_own_origin(db_path, req):
    resp = client_for(db_path, req).post("/api/check-now", headers={**HEADERS, "Origin": "http://localhost"})
    assert resp.status_code == 202


def test_check_now_refused_when_the_monitor_is_not_running(db_path, req):
    resp = client_for(db_path, req, now=NOW + 3600).post("/api/check-now", headers=HEADERS)
    assert resp.status_code == 503 and not req.exists()


def test_check_now_refused_while_pending_or_running(db_path, req):
    c = client_for(db_path, req)
    assert c.post("/api/check-now", headers=HEADERS).status_code == 202
    again = c.post("/api/check-now", headers=HEADERS)
    assert again.status_code == 409 and again.get_json()["state"] == "pending"
    req.unlink()
    set_run(db_path, "running", started_ts=NOW)
    assert c.post("/api/check-now", headers=HEADERS).status_code == 409


def test_check_now_has_a_cooldown(db_path, req):
    set_run(db_path, "idle", started_ts=NOW - 12, finished_ts=NOW - 10)
    resp = client_for(db_path, req).post("/api/check-now", headers=HEADERS)
    assert resp.status_code == 429 and resp.get_json()["retry_after_s"] == settings.CHECK_COOLDOWN_S - 10
    assert not req.exists()
    conn = db.connect(db_path)
    with conn:  # the monitor keeps polling while we wait out the cooldown
        conn.execute("UPDATE monitor_state SET updated_ts = ? WHERE key = 'last_poll'", (NOW + settings.CHECK_COOLDOWN_S,))
    conn.close()
    ok = client_for(db_path, req, now=NOW + settings.CHECK_COOLDOWN_S).post("/api/check-now", headers=HEADERS)
    assert ok.status_code == 202


def test_check_now_can_be_switched_off(db_path, req):
    c = client_for(db_path, req, make_cfg(allow=False))
    assert c.post("/api/check-now", headers=HEADERS).status_code == 404
    assert not req.exists()
    assert c.get("/api/readiness").get_json()["allow_check_now"] is False


def test_check_now_reports_an_unwritable_location(db_path, tmp_path):
    resp = client_for(db_path, tmp_path / "missing-dir" / "req").post("/api/check-now", headers=HEADERS)
    assert resp.status_code == 500 and "could not create" in resp.get_json()["error"]


def test_check_now_without_a_database_is_a_clean_503(tmp_path, req):
    resp = client_for(tmp_path / "missing.db", req).post("/api/check-now", headers=HEADERS)
    assert resp.status_code == 503 and not req.exists()


def test_get_is_not_allowed_on_check_now_and_other_routes_stay_read_only(db_path, req):
    c = client_for(db_path, req)
    assert c.get("/api/check-now").status_code == 405
    for path in ("/", "/api/current", "/api/readiness", "/api/history", "/api/events", "/healthz"):
        assert c.post(path, headers=HEADERS).status_code == 405


def test_page_has_the_readiness_panel_and_button(db_path, req):
    html = client_for(db_path, req).get("/").get_data(as_text=True)
    for element_id in ("readiness-panel", "rd-check", "rd-body", "rd-summary", "rd-status"):
        assert f'id="{element_id}"' in html
    assert "Check now" in html


def test_javascript_sends_the_header_and_never_uses_inner_html():
    text = (settings.STATIC_DIR / "app.js").read_text()
    assert "X-Requested-With" in text and "ups-monitor-ui" in text
    assert "innerHTML" not in text
