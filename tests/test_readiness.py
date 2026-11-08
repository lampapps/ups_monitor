import sqlite3
import threading

import pytest

from app import db, readiness, settings
from app.config import MachineConfig, ShutdownConfig
from app.preflight_scheduler import PreflightScheduler
from app.shutdown import StepResult, TargetCheck, _clean_hostname, preflight
from tests.test_shutdown import FakeRunner, cfg_of

LOGIN = ("hostname", 0, "imac-1\n")
SUDO_L = ("-n -l", 1, "sudo: a password is required")
DRY = "--dry-run"


def host(**spec):
    return {"down_after": None, "by_command": [LOGIN], **spec}


def ssh_commands(runner):
    return [c[2] for c in runner.calls if c[0] == "ssh"]


# ---------------------------------------------------------------- staged check


def test_everything_passes():
    runner = FakeRunner(**{"a.lan": host()})
    a, me = preflight(cfg_of("a"), runner, accept_new=True)
    assert (a.name, a.position, a.remote_hostname, a.overall) == ("a", 1, "imac-1", "ready")
    assert [getattr(a, s).ok for s in ("ping", "ssh", "sudo", "dry")] == [True] * 4
    assert ssh_commands(runner) == [
        settings.HOSTNAME_COMMAND, settings.SUDO_CHECK_COMMAND, settings.DRYRUN_COMMAND,
    ]
    assert all(c[3] is True for c in runner.calls if c[0] == "ssh")
    assert (me.name, me.position, me.ping, me.ssh) == ("this host", 2, None, None)
    assert me.overall == "ready"


def test_a_real_poweroff_is_never_issued():
    runner = FakeRunner(**{"a.lan": host()})
    preflight(cfg_of("a"), runner)
    assert settings.POWEROFF_COMMAND not in ssh_commands(runner)
    assert all(c[1] != ["sudo", "-n", "/usr/bin/systemctl", "poweroff"] for c in runner.calls if c[0] == "local")


def test_unreachable_host_fails_the_ping_step():
    runner = FakeRunner(**{"a.lan": host(up=False)})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.ping.ok is False and a.overall == "attention" and a.detail.startswith("ping:")


def test_ssh_failure_skips_the_rest_and_hides_the_address():
    out = "ssh: connect to host a.lan port 22: Connection timed out"
    runner = FakeRunner(**{"a.lan": host(by_command=[("hostname", 255, out)])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.ssh.ok is False and "connect to host a port 22" in a.ssh.detail and "a.lan" not in a.ssh.detail
    assert a.sudo.ok is None and a.dry.ok is None and "skipped" in a.sudo.detail
    assert a.overall == "attention" and ssh_commands(runner) == [settings.HOSTNAME_COMMAND]


def test_user_at_host_is_scrubbed_from_details():
    out = "u@a.lan: Permission denied (publickey)."
    runner = FakeRunner(**{"a.lan": host(by_command=[("hostname", 255, out)])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert "a.lan" not in a.ssh.detail and "a: Permission denied" in a.ssh.detail


def test_sudo_rule_failure_is_required():
    runner = FakeRunner(**{"a.lan": host(by_command=[LOGIN, SUDO_L])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.ssh.ok and a.sudo.ok is False and a.overall == "attention"
    assert "password is required" in a.sudo.detail and a.detail.startswith("sudo:")


def test_dry_run_not_enabled_is_only_a_partial_result():
    runner = FakeRunner(**{"a.lan": host(by_command=[LOGIN, (DRY, 1, "sudo: a password is required")])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.sudo.ok is True and a.dry.ok is None and "re-run setup-target.sh on that machine" in a.dry.detail
    assert a.overall == "partial" and a.ok is True


def test_dry_run_unsupported_systemctl_is_partial():
    runner = FakeRunner(**{"a.lan": host(by_command=[LOGIN, (DRY, 1, "systemctl: unrecognized option '--dry-run'")])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.dry.ok is None and "not supported" in a.dry.detail and a.overall == "partial"


def test_dry_run_real_failure_needs_attention():
    runner = FakeRunner(**{"a.lan": host(by_command=[LOGIN, (DRY, 1, "Failed to connect to bus")])})
    a = preflight(cfg_of("a", shutdown_self=False), runner)[0]
    assert a.dry.ok is False and a.overall == "attention" and "Failed to connect to bus" in a.dry.detail


def test_custom_command_only_proves_ssh():
    cfg = ShutdownConfig(
        dry_run=False, shutdown_self=False,
        machines=[MachineConfig(name="a", host="a.lan", user="u", command="sudo -n /sbin/poweroff")],
    )
    runner = FakeRunner(**{"a.lan": host()})
    a = preflight(cfg, runner)[0]
    assert a.ssh.ok and a.sudo.ok is None and a.dry.ok is None and a.overall == "partial"
    assert "custom command" in a.sudo.detail
    assert ssh_commands(runner) == [settings.HOSTNAME_COMMAND]


def test_this_host_checks_sudo_and_dry_run_locally():
    runner = FakeRunner()
    runner.local_results = [("-n -l", 1, "sudo: a password is required")]
    (me,) = preflight(cfg_of(), runner)
    assert me.sudo.ok is False and me.overall == "attention"
    runner = FakeRunner()
    runner.local_results = [(DRY, 1, "sudo: a password is required")]
    (me,) = preflight(cfg_of(), runner)
    assert me.sudo.ok is True and me.dry.ok is None and me.overall == "partial"
    assert "deploy/install.sh" in me.dry.detail and "setup-target.sh" not in me.dry.detail


def test_order_and_names():
    runner = FakeRunner(**{"a.lan": host(), "b.lan": host()})
    checks = preflight(cfg_of("a", "b"), runner)
    assert [(c.position, c.name) for c in checks] == [(1, "a"), (2, "b"), (3, "this host")]
    assert [c.name for c in preflight(cfg_of("a", "b", shutdown_self=False), FakeRunner(**{"a.lan": host(), "b.lan": host()}))] == ["a", "b"]


def test_detail_summarises_a_healthy_target():
    check = TargetCheck("a", 1, StepResult(True, "x"), StepResult(True, "x"), StepResult(True, "x"), StepResult(None, "not enabled"))
    assert check.detail == "ping OK, ssh OK, sudo OK, dry-run not enabled"


@pytest.mark.parametrize(
    "output, expected",
    [
        ("imac-1\n", "imac-1"),
        ("Warning: Permanently added 'a.lan' (ED25519) to the list of known hosts.\nimac-1", "imac-1"),
        ("host.example.net", "host.example.net"),
        ("", None),
        ("<script>alert(1)</script>", None),
        ("two words", None),
        ("x" * 100, None),
        ("-leading-dash", None),
    ],
)
def test_remote_hostname_is_sanitised(output, expected):
    assert _clean_hostname(output) == expected


# ---------------------------------------------------------------- stored results


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ups.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


def sample_checks():
    ok = StepResult(True, "fine")
    return [
        TargetCheck("a", 1, ok, ok, ok, StepResult(None, "not enabled"), "imac-1"),
        TargetCheck("this host", 2, None, None, ok, ok),
    ]


def test_save_and_load(conn):
    readiness.save(conn, sample_checks(), 1000, "daily")
    a, me = readiness.load(conn)
    assert (a["name"], a["position"], a["remote_hostname"], a["overall"], a["checked_ts"], a["trigger"]) == (
        "a", 1, "imac-1", "partial", 1000, "daily")
    assert (a["ssh_ok"], a["dry_ok"], a["dry_detail"]) == (1, None, "not enabled")
    assert (me["ping_ok"], me["ping_detail"], me["sudo_ok"]) == (None, None, 1)


def test_save_replaces_and_drops_removed_machines(conn):
    readiness.save(conn, sample_checks(), 1000, "daily")
    readiness.save(conn, sample_checks()[:1], 2000, "manual")
    rows = readiness.load(conn)
    assert [(r["name"], r["checked_ts"], r["trigger"]) for r in rows] == [("a", 2000, "manual")]


def test_prune(conn):
    readiness.save(conn, sample_checks(), 1000, "daily")
    readiness.prune(conn, ["this host"])
    assert [r["name"] for r in readiness.load(conn)] == ["this host"]
    readiness.prune(conn, [])
    assert readiness.load(conn) == []


def test_run_state_round_trip(conn):
    assert readiness.get_run(conn)["state"] == "idle"
    readiness.set_run(conn, 5, "running", "manual", started_ts=5)
    run = readiness.get_run(conn)
    assert (run["state"], run["trigger"], run["started_ts"], run["finished_ts"]) == ("running", "manual", 5, None)
    readiness.set_run(conn, 9, "idle", "manual", started_ts=5, finished_ts=9, note="x")
    assert readiness.get_run(conn)["finished_ts"] == 9 and readiness.get_run(conn)["note"] == "x"


# ---------------------------------------------------------------- scheduler


class Events(list):
    def __call__(self, *args):
        self.append(args)

    def types(self):
        return [e[1] for e in self]


def make(db_path, tmp_path, runner, events=None, cfg=None, **kw):
    events = Events() if events is None else events
    kw.setdefault("poll_s", 0.01)
    kw.setdefault("first_delay_s", 0)
    s = PreflightScheduler(
        cfg or cfg_of("a", shutdown_self=False, preflight_interval_hours=0.00001), runner, events,
        conn_factory=lambda: db.connect(db_path), request_path=tmp_path / "req", **kw,
    )
    return s, events


def healthy():
    return FakeRunner(**{"a.lan": host()})


def broken():
    return FakeRunner(**{"a.lan": host(by_command=[LOGIN, SUDO_L])})


def stored(db_path):
    c = db.connect(db_path)
    try:
        return readiness.load(c), readiness.get_run(c)
    finally:
        c.close()


def test_run_once_stores_results_and_run_state(db_path, tmp_path):
    s, events = make(db_path, tmp_path, healthy())
    assert s.run_once("manual") is True
    rows, run = stored(db_path)
    assert [(r["name"], r["overall"], r["trigger"]) for r in rows] == [("a", "ready", "manual")]
    assert run["state"] == "idle" and run["trigger"] == "manual" and run["finished_ts"] and run["note"] is None
    assert events == []


def test_state_is_running_while_the_check_executes(db_path, tmp_path):
    seen = []
    runner = healthy()
    runner.on_ssh = lambda m: seen.append(stored(db_path)[1]["state"])
    make(db_path, tmp_path, runner)[0].run_once("manual")
    assert seen and set(seen) == {"running"}


def test_skipped_while_a_shutdown_sequence_runs(db_path, tmp_path):
    runner = healthy()
    s, events = make(db_path, tmp_path, runner, busy=lambda: True)
    assert s.run_once("manual") is False
    rows, run = stored(db_path)
    assert runner.calls == [] and rows == [] and "shutdown sequence" in run["note"] and run["state"] == "idle"


def test_an_exception_never_leaves_the_state_stuck_on_running(db_path, tmp_path):
    class Boom(FakeRunner):
        def ping(self, h):
            raise RuntimeError("boom")

    s, _ = make(db_path, tmp_path, Boom(**{"a.lan": host()}))
    with pytest.raises(RuntimeError):
        s.run_once("manual")
    run = stored(db_path)[1]
    assert run["state"] == "idle" and "did not complete" in run["note"]


def test_healthy_targets_send_nothing(db_path, tmp_path):
    s, events = make(db_path, tmp_path, healthy())
    s.run_once("daily")
    s.run_once("manual")
    assert events == []


def test_failure_is_reported_by_name_and_repeated_by_the_daily_run(db_path, tmp_path):
    s, events = make(db_path, tmp_path, broken())
    s.run_once("daily")
    assert events.types() == ["preflight_failed"]
    _, _, severity, message, data = events[0]
    assert severity == "warning" and "a: sudo" in message and "a.lan" not in message
    assert data == {"failed": ["a"], "trigger": "daily"}
    s.run_once("daily")
    assert events.types() == ["preflight_failed", "preflight_failed"]


def test_repeated_manual_checks_do_not_repeat_the_alert(db_path, tmp_path):
    s, events = make(db_path, tmp_path, broken())
    s.run_once("manual")
    assert events.types() == ["preflight_failed"]
    s.run_once("manual")
    s.run_once("cli")
    assert events.types() == ["preflight_failed"]


def test_recovery_is_reported_once(db_path, tmp_path):
    runner = broken()
    s, events = make(db_path, tmp_path, runner)
    s.run_once("daily")
    runner.hosts["a.lan"]["by_command"] = [LOGIN]
    s.run_once("manual")
    s.run_once("manual")
    assert events.types() == ["preflight_failed", "preflight_ok"]


def test_request_file_triggers_exactly_one_manual_check(db_path, tmp_path):
    stop = threading.Event()
    runner = healthy()
    runner.on_ssh = lambda m: None
    s, _ = make(db_path, tmp_path, runner, cfg=cfg_of("a", shutdown_self=False, preflight_interval_hours=0))
    runs = []
    original = s.run_once
    s.run_once = lambda trigger="daily": (runs.append(trigger), original(trigger), stop.set())[1]
    t = threading.Thread(target=s.run, args=(stop,))
    t.start()
    stop.wait(0.2)  # the request must arrive after startup, which discards leftovers
    (tmp_path / "req").write_text("")
    t.join(5)
    assert not t.is_alive() and runs == ["manual"] and not (tmp_path / "req").exists()
    assert stored(db_path)[0][0]["trigger"] == "manual"


def test_scheduled_runs_are_startup_then_daily(db_path, tmp_path):
    stop = threading.Event()
    s, _ = make(db_path, tmp_path, healthy())
    runs = []
    original = s.run_once
    s.run_once = lambda trigger="daily": (runs.append(trigger), original(trigger), len(runs) >= 3 and stop.set())[1]
    t = threading.Thread(target=s.run, args=(stop,))
    t.start()
    t.join(5)
    assert not t.is_alive() and runs[:3] == ["startup", "daily", "daily"]


def test_interval_zero_disables_only_the_schedule(db_path, tmp_path):
    stop = threading.Event()
    runner = healthy()
    s, _ = make(db_path, tmp_path, runner, cfg=cfg_of("a", shutdown_self=False, preflight_interval_hours=0))
    t = threading.Thread(target=s.run, args=(stop,))
    t.start()
    stop.wait(0.15)
    assert runner.calls == []
    (tmp_path / "req").write_text("")
    for _ in range(100):
        if stored(db_path)[0]:
            break
        stop.wait(0.05)
    stop.set()
    t.join(5)
    assert stored(db_path)[0][0]["trigger"] == "manual"


def test_startup_cleans_up_after_a_crash(db_path, tmp_path):
    c = db.connect(db_path)
    readiness.save(c, [TargetCheck("removed-machine", 1)], 1, "daily")
    readiness.set_run(c, 1, "running", "manual", started_ts=1)
    c.close()
    (tmp_path / "req").write_text("")
    stop = threading.Event()
    stop.set()
    s, _ = make(db_path, tmp_path, healthy(), cfg=cfg_of("a", shutdown_self=False, preflight_interval_hours=0))
    s.run(stop)
    rows, run = stored(db_path)
    assert rows == [] and run["state"] == "idle" and run["note"] == "monitor restarted"
    assert not (tmp_path / "req").exists()


def test_loop_survives_a_failing_check(db_path, tmp_path):
    stop = threading.Event()
    calls = []

    class Flaky(FakeRunner):
        def ping(self, h):
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            raise RuntimeError("boom")

    s, _ = make(db_path, tmp_path, Flaky(**{"a.lan": host()}))
    t = threading.Thread(target=s.run, args=(stop,))
    t.start()
    t.join(5)
    assert not t.is_alive() and len(calls) >= 2


def test_database_problems_do_not_stop_the_check_or_the_alert(tmp_path):
    def no_db():
        raise sqlite3.OperationalError("unable to open database file")

    events = Events()
    s = PreflightScheduler(cfg_of("a", shutdown_self=False), broken(), events, conn_factory=no_db,
                           request_path=tmp_path / "req")
    assert s.run_once("daily") is True
    assert events.types() == ["preflight_failed"]
    s = PreflightScheduler(cfg_of("a", shutdown_self=False), healthy(), events, conn_factory=None,
                           request_path=tmp_path / "req2")
    assert s.run_once("manual") is True
