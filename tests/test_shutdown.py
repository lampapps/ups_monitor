import subprocess
import threading
from types import SimpleNamespace

import pytest

from app import db, settings, shutdown
from app.config import MachineConfig, ShutdownConfig
from app.poller import PollResult
from app.shutdown import (
    CommandResult,
    ShutdownEngine,
    ShutdownTrigger,
    SubprocessRunner,
)


# ---------------------------------------------------------------- fakes


class FakeRunner:
    """Scripted hosts: ssh_rc, and down_after = pings that still answer once ssh has run."""

    def __init__(self, **hosts):
        self.hosts = {
            h: {"up": True, "ssh_rc": 0, "down_after": 0, "countdown": None, "output": "", **spec}
            for h, spec in hosts.items()
        }
        self.calls = []
        self.local_rc = 0
        self.on_ssh = None
        self.local_results = []

    def ssh(self, m, command, *, accept_new=False):
        self.calls.append(("ssh", m.name, command, accept_new))
        h = self.hosts[m.host]
        if h["countdown"] is None and h["down_after"] is not None:
            h["countdown"] = h["down_after"]
        if self.on_ssh:
            self.on_ssh(m)
        for needle, rc, out in h.get("by_command", []):
            if needle in command:
                return CommandResult(rc, out)
        return CommandResult(h["ssh_rc"], h["output"])

    def ping(self, host):
        h = self.hosts[host]
        if h["countdown"] is not None:
            if h["countdown"] <= 0:
                h["up"] = False
            else:
                h["countdown"] -= 1
        return h["up"]

    def local(self, argv):
        self.calls.append(("local", argv))
        for needle, rc, out in self.local_results:
            if needle in " ".join(argv):
                return CommandResult(rc, out)
        return CommandResult(self.local_rc, "")

    def ssh_names(self):
        return [c[1] for c in self.calls if c[0] == "ssh"]


class ForbiddenRunner:
    def __getattr__(self, name):
        raise AssertionError(f"runner.{name} must not be used in dry run")


class Clock:
    now = 0.0

    def advance(self, s):
        self.now += s


def machine(name, **kw):
    return MachineConfig(name=name, host=f"{name}.lan", user="u", wait_for_down_s=kw.pop("wait", 10), **kw)


def cfg_of(*names, **kw):
    kw.setdefault("dry_run", False)
    return ShutdownConfig(machines=[machine(n) for n in names], **kw)


def make_engine(cfg, runner, conn_factory=None):
    events, clock = [], Clock()
    holder = {}

    def wait(seconds):
        clock.advance(seconds)
        return holder["engine"].abort_requested

    engine = ShutdownEngine(
        cfg, runner, lambda *a: events.append(a), conn_factory,
        clock=lambda: 1000 + clock.now, monotonic=lambda: clock.now, wait=wait,
    )
    holder["engine"] = engine
    return engine, events, clock


def event_types(events):
    return [e[1] for e in events]


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "ups.db"
    db.init_db(p)
    return p


def rows(db_path, sql):
    conn = db.connect(db_path)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


# ---------------------------------------------------------------- engine


def test_dry_run_never_touches_the_runner(db_path):
    cfg = cfg_of("a", "b", dry_run=True)
    engine, events, _ = make_engine(cfg, ForbiddenRunner(), lambda: db.connect(db_path))
    engine.run_sequence("test reason")
    assert event_types(events) == ["shutdown_started", "shutdown_step", "shutdown_step", "shutdown_completed"]
    assert events[0][3].startswith("DRY RUN")
    assert rows(db_path, "SELECT state, dry_run, reason FROM shutdown_runs") == [("completed", 1, "test reason")]
    steps = rows(db_path, "SELECT machine, state, detail FROM shutdown_steps ORDER BY seq")
    assert [s[:2] for s in steps] == [("a", "done"), ("b", "done"), ("this host", "done")]
    assert all("dry run" in s[2] for s in steps)


def test_machines_shut_down_in_order_then_self_last(db_path):
    runner = FakeRunner(**{"a.lan": {}, "b.lan": {}})
    engine, events, _ = make_engine(cfg_of("a", "b"), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert [c[0] for c in runner.calls] == ["ssh", "ssh", "local"]
    assert runner.ssh_names() == ["a", "b"]
    assert runner.calls[0][2] == settings.POWEROFF_COMMAND
    assert runner.calls[2][1] == ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]
    assert rows(db_path, "SELECT state FROM shutdown_runs") == [("completed",)]
    assert [r[0] for r in rows(db_path, "SELECT state FROM shutdown_steps ORDER BY seq")] == ["done"] * 3
    assert "powering off this host now" in events[-1][3]


def test_wait_for_host_to_go_down_before_next_machine():
    runner = FakeRunner(**{"a.lan": {"down_after": 3}, "b.lan": {}})
    engine, _, clock = make_engine(cfg_of("a", "b"), runner)
    engine.run_sequence("r")
    assert clock.now == 3 * settings.PING_INTERVAL_S


def test_custom_command_is_used():
    cfg = ShutdownConfig(
        dry_run=False, shutdown_self=False,
        machines=[MachineConfig(name="a", host="a.lan", user="u", command="sudo -n /sbin/poweroff")],
    )
    runner = FakeRunner(**{"a.lan": {}})
    make_engine(cfg, runner)[0].run_sequence("r")
    assert runner.calls[0][2] == "sudo -n /sbin/poweroff"


def test_ssh_failure_with_host_down_is_skipped_and_sequence_continues(db_path):
    runner = FakeRunner(**{"a.lan": {"up": False, "ssh_rc": 255, "down_after": None}, "b.lan": {}})
    engine, events, _ = make_engine(cfg_of("a", "b"), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert runner.ssh_names() == ["a", "b"]
    states = rows(db_path, "SELECT machine, state, detail FROM shutdown_steps ORDER BY seq")
    assert states[0][:2] == ("a", "skipped") and "assumed already down" in states[0][2]
    assert states[1][1] == "done" and runner.calls[-1][0] == "local"


def test_remote_command_failure_is_retried_then_reported_and_sequence_continues(db_path):
    runner = FakeRunner(**{"a.lan": {"ssh_rc": 1, "output": "sudo: a password is required", "down_after": None},
                           "b.lan": {}})
    engine, events, _ = make_engine(cfg_of("a", "b"), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert runner.ssh_names() == ["a"] * settings.SSH_ATTEMPTS + ["b"]
    state, detail = rows(db_path, "SELECT state, detail FROM shutdown_steps WHERE seq = 0")[0]
    assert state == "failed" and "password is required" in detail
    failure = [e for e in events if e[1] == "shutdown_step" and not e[4]["ok"]]
    assert failure and failure[0][2] == "critical"
    assert runner.calls[-1][0] == "local"


def test_ssh_session_cut_by_poweroff_counts_as_done(db_path):
    runner = FakeRunner(**{"a.lan": {"ssh_rc": 255, "down_after": 3}})
    engine, _, _ = make_engine(cfg_of("a", shutdown_self=False), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert rows(db_path, "SELECT state FROM shutdown_steps") == [("done",)]


def test_host_that_never_goes_down_fails_after_timeout_and_sequence_continues(db_path):
    runner = FakeRunner(**{"a.lan": {"down_after": None}, "b.lan": {}})
    engine, events, clock = make_engine(cfg_of("a", "b"), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    state, detail = rows(db_path, "SELECT state, detail FROM shutdown_steps WHERE seq = 0")[0]
    assert state == "failed" and "still answers ping after 10 s" in detail
    assert runner.ssh_names() == ["a", "b"]
    assert clock.now >= 10


def test_abort_between_steps_skips_the_rest_including_self(db_path):
    runner = FakeRunner(**{"a.lan": {}, "b.lan": {}})
    engine, events, _ = make_engine(cfg_of("a", "b"), runner, lambda: db.connect(db_path))
    runner.on_ssh = lambda m: engine.request_abort("power restored")
    engine.run_sequence("r")
    assert runner.ssh_names() == ["a"]
    assert not any(c[0] == "local" for c in runner.calls)
    assert rows(db_path, "SELECT state FROM shutdown_runs") == [("aborted",)]
    assert [r[0] for r in rows(db_path, "SELECT state FROM shutdown_steps ORDER BY seq")] == [
        "done", "skipped", "skipped",
    ]
    aborted = [e for e in events if e[1] == "shutdown_aborted"][0]
    assert "power restored" in aborted[3] and aborted[4]["already_shut_down"] == ["a"]


def test_self_poweroff_failure_is_reported(db_path):
    runner = FakeRunner()
    runner.local_rc = 1
    engine, events, _ = make_engine(cfg_of(), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert rows(db_path, "SELECT state FROM shutdown_steps") == [("failed",)]
    last = events[-1]
    assert last[1] == "shutdown_step" and last[2] == "critical" and "could not power off" in last[3]


def test_self_not_shut_down_when_disabled(db_path):
    runner = FakeRunner(**{"a.lan": {}})
    engine, events, _ = make_engine(cfg_of("a", shutdown_self=False), runner, lambda: db.connect(db_path))
    engine.run_sequence("r")
    assert not any(c[0] == "local" for c in runner.calls)
    assert rows(db_path, "SELECT state FROM shutdown_runs") == [("completed",)]
    assert events[-1][1] == "shutdown_completed"


def test_database_failure_does_not_stop_the_sequence(db_path):
    def broken():
        conn = db.connect(db_path)
        conn.close()
        return conn

    runner = FakeRunner(**{"a.lan": {}})
    make_engine(cfg_of("a"), runner, broken)[0].run_sequence("r")
    assert [c[0] for c in runner.calls] == ["ssh", "local"]


def test_no_database_at_all():
    runner = FakeRunner(**{"a.lan": {}})
    make_engine(cfg_of("a"), runner, None)[0].run_sequence("r")
    assert runner.calls[-1][0] == "local"


def test_recover_closes_stale_runs(db_path):
    conn = db.connect(db_path)
    with conn:
        conn.execute("INSERT INTO shutdown_runs (id, started_ts, state, dry_run, reason) VALUES (1, 1, 'running', 0, 'x')")
        conn.execute("INSERT INTO shutdown_steps (run_id, seq, machine, state) VALUES (1, 0, 'a', 'done')")
        conn.execute("INSERT INTO shutdown_steps (run_id, seq, machine, state) VALUES (1, 1, 'b', 'pending')")
    conn.close()
    engine, events, _ = make_engine(cfg_of("a", "b"), FakeRunner(), lambda: db.connect(db_path))
    engine.recover()
    assert rows(db_path, "SELECT state FROM shutdown_runs") == [("aborted",)]
    assert [r[0] for r in rows(db_path, "SELECT state FROM shutdown_steps ORDER BY seq")] == ["done", "skipped"]
    assert event_types(events) == ["shutdown_interrupted"]
    engine.recover()
    assert event_types(events) == ["shutdown_interrupted"]


def test_start_runs_in_a_thread_and_refuses_a_second_run():
    entered, gate = threading.Event(), threading.Event()

    class Blocking(FakeRunner):
        def ssh(self, m, command, *, accept_new=False):
            entered.set()
            gate.wait(5)
            return super().ssh(m, command, accept_new=accept_new)

    cfg = cfg_of("a", shutdown_self=False)
    engine = ShutdownEngine(cfg, Blocking(**{"a.lan": {}}), lambda *a: None)
    assert engine.start("r") is True
    assert entered.wait(5) and engine.running
    assert engine.start("again") is False
    gate.set()
    engine._thread.join(5)
    assert not engine.running


def test_close_aborts_a_running_sequence():
    entered = threading.Event()

    class Slow(FakeRunner):
        def ssh(self, m, command, *, accept_new=False):
            entered.set()
            return super().ssh(m, command, accept_new=accept_new)

    cfg = cfg_of("a", "b")
    runner = Slow(**{"a.lan": {"down_after": None}, "b.lan": {}})
    engine = ShutdownEngine(cfg, runner, lambda *a: None)
    engine.start("r")
    assert entered.wait(5)
    engine.close(timeout=5)
    assert not engine.running
    assert runner.ssh_names() == ["a"]
    assert not any(c[0] == "local" for c in runner.calls)


# ---------------------------------------------------------------- trigger


class FakeEngine:
    def __init__(self):
        self.running = False
        self.started, self.aborts = [], []

    def start(self, reason):
        self.started.append(reason)
        self.running = True

    def request_abort(self, reason):
        self.aborts.append(reason)


def poll(status="OB DISCHRG", runtime="300"):
    values = {"ups.status": status}
    if runtime is not None:
        values["battery.runtime"] = runtime
    return PollResult(ts=0, ok=True, status=status, values=values)


GAP = PollResult(ts=0, ok=False, error_kind="unreachable", error="x")


def trigger(**kw):
    kw.setdefault("runtime_threshold_minutes", 10)
    kw.setdefault("consecutive_polls", 3)
    engine = FakeEngine()
    return ShutdownTrigger(ShutdownConfig(**kw), engine), engine


def feed(t, *results):
    for r in results:
        t(r)


def test_fires_on_nth_consecutive_qualifying_poll():
    t, e = trigger()
    feed(t, poll(), poll())
    assert e.started == []
    t(poll())
    assert len(e.started) == 1 and "5.0 min runtime left" in e.started[0]


def test_runtime_above_threshold_never_fires():
    t, e = trigger()
    feed(t, *[poll(runtime="601")] * 10)
    assert e.started == []


def test_boundary_runtime_equal_to_threshold_fires():
    t, e = trigger()
    feed(t, *[poll(runtime="600")] * 3)
    assert len(e.started) == 1


def test_online_poll_resets_the_count():
    t, e = trigger()
    feed(t, poll(), poll(), poll("OL"), poll(), poll())
    assert e.started == []


def test_failed_polls_neither_count_nor_reset():
    t, e = trigger()
    feed(t, GAP, GAP, GAP, GAP, GAP)
    assert e.started == []
    feed(t, poll(), GAP, poll(), GAP, GAP)
    assert e.started == []
    t(poll())
    assert len(e.started) == 1


def test_self_test_is_never_a_trigger():
    t, e = trigger()
    feed(t, *[poll("OB CAL", runtime="60")] * 10)
    assert e.started == []


def test_low_battery_flag_fires_regardless_of_runtime():
    t, e = trigger()
    feed(t, *[poll("OB DISCHRG LB", runtime="9999")] * 3)
    assert "low battery" in e.started[0]


def test_missing_runtime_without_lb_does_not_fire():
    t, e = trigger()
    feed(t, *[poll(runtime=None)] * 10)
    assert e.started == []


def test_disabled_never_fires():
    t, e = trigger(enabled=False)
    feed(t, *[poll()] * 10)
    assert e.started == []


def test_does_not_fire_again_while_running_and_aborts_after_stable_power():
    t, e = trigger()
    feed(t, *[poll()] * 3)
    assert len(e.started) == 1
    feed(t, poll(), poll())
    assert len(e.started) == 1 and e.aborts == []
    feed(t, poll("OL"), poll("OL"))
    assert e.aborts == []
    t(poll("OL"))
    assert e.aborts == ["power restored"]


def test_power_flicker_does_not_abort():
    t, e = trigger()
    feed(t, *[poll()] * 3)
    feed(t, poll("OL"), poll("OL"), poll(), poll("OL"), poll("OL"))
    assert e.aborts == []


def test_online_polls_without_running_sequence_do_nothing():
    t, e = trigger()
    feed(t, *[poll("OL")] * 10)
    assert e.started == [] and e.aborts == []


# ---------------------------------------------------------------- real runner argv


def test_guard_blocks_real_processes():
    with pytest.raises(AssertionError, match="real process"):
        SubprocessRunner().ping("x")


def test_ssh_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(
        shutdown, "_spawn", lambda argv, timeout: seen.append((argv, timeout)) or SimpleNamespace(returncode=0, stdout="o", stderr="e")
    )
    m = MachineConfig(name="a", host="10.0.0.5", user="upsshutdown", port=2222)
    res = SubprocessRunner().ssh(m, settings.POWEROFF_COMMAND)
    argv, timeout = seen[0]
    assert res == CommandResult(0, "oe")
    assert argv[0] == "ssh" and isinstance(argv, list)
    assert argv[-2:] == ["upsshutdown@10.0.0.5", settings.POWEROFF_COMMAND]
    joined = " ".join(argv)
    for expected in ("BatchMode=yes", "StrictHostKeyChecking=yes", "-p 2222", str(settings.SSH_KEY_PATH),
                     f"UserKnownHostsFile={settings.KNOWN_HOSTS_PATH}"):
        assert expected in joined
    assert timeout > settings.SSH_CONNECT_TIMEOUT_S
    SubprocessRunner().ssh(m, "true", accept_new=True)
    assert "StrictHostKeyChecking=accept-new" in " ".join(seen[1][0])


def test_ping_and_failures(monkeypatch):
    calls = []

    def fake(argv, timeout):
        calls.append(argv)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(argv, timeout)
        if len(calls) == 3:
            raise FileNotFoundError("no ssh")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(shutdown, "_spawn", fake)
    r = SubprocessRunner()
    assert r.ping("h") is False and calls[0] == ["ping", "-c", "1", "-W", "1", "h"]
    assert r.local(["x"]) == CommandResult(124, "timed out")
    assert r.local(["x"]).returncode == 127


# ---------------------------------------------------------------- before_poweroff hook


def engine_with_hook(runner, hook, **cfg_kw):
    engine, events, _ = make_engine(cfg_of(**cfg_kw), runner)
    engine._before_poweroff = hook
    return engine


def test_hook_runs_before_local_poweroff():
    runner = FakeRunner()
    engine = engine_with_hook(runner, lambda: runner.calls.append(("hook",)))
    engine.run_sequence("r")
    assert [c[0] for c in runner.calls] == ["hook", "local"]


def test_hook_failure_does_not_prevent_poweroff():
    runner = FakeRunner()

    def boom():
        raise RuntimeError("smtp down")

    engine_with_hook(runner, boom).run_sequence("r")
    assert runner.calls[-1][0] == "local"


def test_hook_not_called_in_dry_run():
    called = []
    engine = engine_with_hook(ForbiddenRunner(), lambda: called.append(1), dry_run=True)
    engine.run_sequence("r")
    assert called == []
