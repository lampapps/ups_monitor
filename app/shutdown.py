"""Shutdown logic: trigger decision, ordered execution over SSH, preflight checks.

Safety rules: dry_run (the default) never calls the runner; tests replace `_spawn` so no
real process is ever started; failed polls never count towards a trigger.
"""

import logging
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import settings
from .config import MachineConfig, ShutdownConfig
from .poller import PollResult

log = logging.getLogger(__name__)

Emit = Callable[[int, str, str, str, dict], None]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str = ""


class Runner(Protocol):
    def ssh(self, machine: MachineConfig, command: str, *, accept_new: bool = False) -> CommandResult: ...
    def ping(self, host: str) -> bool: ...
    def local(self, argv: list[str]) -> CommandResult: ...


def _spawn(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    # The single place a process is started; tests replace it.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


class SubprocessRunner:
    def ssh(self, machine: MachineConfig, command: str, *, accept_new: bool = False) -> CommandResult:
        argv = [
            "ssh",
            "-i", str(settings.SSH_KEY_PATH),
            "-p", str(machine.port),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", f"ConnectTimeout={settings.SSH_CONNECT_TIMEOUT_S}",
            "-o", f"UserKnownHostsFile={settings.KNOWN_HOSTS_PATH}",
            "-o", f"StrictHostKeyChecking={'accept-new' if accept_new else 'yes'}",
            f"{machine.user}@{machine.host}",
            command,
        ]
        return self._run(argv, settings.SSH_CONNECT_TIMEOUT_S + settings.SSH_COMMAND_TIMEOUT_S)

    def ping(self, host: str) -> bool:
        return self._run(["ping", "-c", "1", "-W", "1", host], 5).returncode == 0

    def local(self, argv: list[str]) -> CommandResult:
        return self._run(argv, settings.SSH_COMMAND_TIMEOUT_S)

    @staticmethod
    def _run(argv: list[str], timeout: float) -> CommandResult:
        try:
            proc = _spawn(argv, timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(124, "timed out")
        except OSError as exc:
            return CommandResult(127, str(exc))
        return CommandResult(proc.returncode, (proc.stdout + proc.stderr).strip()[-500:])


# ---------------------------------------------------------------- trigger


def _runtime_seconds(result: PollResult) -> float | None:
    try:
        return float(result.values["battery.runtime"])
    except (KeyError, ValueError):
        return None


class ShutdownTrigger:
    """Poller listener. Starts the engine after N consecutive qualifying polls."""

    def __init__(self, cfg: ShutdownConfig, engine):
        self.cfg = cfg
        self.engine = engine
        self._met = 0
        self._power_ok = 0

    def __call__(self, result: PollResult) -> None:
        if not self.cfg.enabled or not result.ok:
            return  # a failed poll is a gap: it neither counts nor resets

        flags = set((result.status or "").split())
        if self.engine.running:
            self._met = 0
            self._power_ok = self._power_ok + 1 if "OB" not in flags else 0
            if self._power_ok >= self.cfg.consecutive_polls:
                self._power_ok = 0
                self.engine.request_abort("power restored")
            return
        self._power_ok = 0

        reason = self._reason(result, flags)
        if reason is None:
            self._met = 0
            return
        self._met += 1
        log.warning("shutdown condition met (%d/%d): %s", self._met, self.cfg.consecutive_polls, reason)
        if self._met >= self.cfg.consecutive_polls:
            self._met = 0
            self.engine.start(reason)

    def _reason(self, result: PollResult, flags: set[str]) -> str | None:
        # CAL marks the weekly self-test, which is never an outage.
        if "OB" not in flags or "CAL" in flags:
            return None
        if "LB" in flags:
            return "on battery and the UPS reports low battery"
        runtime = _runtime_seconds(result)
        if runtime is not None and runtime <= self.cfg.runtime_threshold_minutes * 60:
            return (
                f"on battery with {runtime / 60:.1f} min runtime left "
                f"(threshold {self.cfg.runtime_threshold_minutes:g} min)"
            )
        return None


# ---------------------------------------------------------------- persistence


class _RunStore:
    """Best-effort bookkeeping; the sequence continues if the database is unavailable."""

    def __init__(self, conn: sqlite3.Connection | None):
        self.conn = conn

    def _exec(self, sql: str, params: tuple = ()) -> int | None:
        if self.conn is None:
            return None
        try:
            with self.conn:
                return self.conn.execute(sql, params).lastrowid
        except sqlite3.Error:
            log.exception("database write failed")
            return None

    def create_run(self, ts: int, dry_run: bool, reason: str, names: list[str]) -> int | None:
        run_id = self._exec(
            "INSERT INTO shutdown_runs (started_ts, state, dry_run, reason) VALUES (?, 'running', ?, ?)",
            (ts, int(dry_run), reason),
        )
        if run_id is not None:
            for seq, name in enumerate(names):
                self._exec(
                    "INSERT INTO shutdown_steps (run_id, seq, machine, state) VALUES (?, ?, ?, 'pending')",
                    (run_id, seq, name),
                )
        return run_id

    def step(self, run_id: int | None, seq: int, state: str, ts: int, detail: str | None = None) -> None:
        if run_id is None:
            return
        self._exec(
            "UPDATE shutdown_steps SET state = ?, detail = COALESCE(?, detail), "
            "started_ts = CASE WHEN ? = 'running' THEN ? ELSE started_ts END, "
            "finished_ts = CASE WHEN ? IN ('done', 'failed', 'skipped') THEN ? ELSE finished_ts END "
            "WHERE run_id = ? AND seq = ?",
            (state, detail, state, ts, state, ts, run_id, seq),
        )

    def finish_run(self, run_id: int | None, state: str, ts: int) -> None:
        if run_id is not None:
            self._exec("UPDATE shutdown_runs SET state = ?, finished_ts = ? WHERE id = ?", (state, ts, run_id))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()

    def recover(self, ts: int) -> int:
        """Close runs left 'running' by a crash or restart. Returns how many."""
        if self.conn is None:
            return 0
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE shutdown_steps SET state = 'skipped', detail = 'monitor restarted', finished_ts = ? "
                    "WHERE state IN ('pending', 'running') "
                    "AND run_id IN (SELECT id FROM shutdown_runs WHERE state = 'running')",
                    (ts,),
                )
                cur = self.conn.execute(
                    "UPDATE shutdown_runs SET state = 'aborted', finished_ts = ? WHERE state = 'running'", (ts,)
                )
                return cur.rowcount
        except sqlite3.Error:
            log.exception("could not recover stale shutdown runs")
            return 0


# ---------------------------------------------------------------- engine

_SELF = "this host"


class ShutdownEngine:
    def __init__(
        self,
        cfg: ShutdownConfig,
        runner: Runner,
        emit: Emit,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        before_poweroff: Callable[[], object] | None = None,
    ):
        self.cfg = cfg
        self._runner = runner
        self._emit = emit
        self._before_poweroff = before_poweroff
        self._conn_factory = conn_factory
        self._clock = clock
        self._monotonic = monotonic
        self._abort = threading.Event()
        self._abort_reason = ""
        # wait(seconds) sleeps and returns True if an abort was requested meanwhile.
        self._wait = wait or self._abort.wait
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- control

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def abort_requested(self) -> bool:
        return self._abort.is_set()

    def start(self, reason: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self._abort.clear()
            self._thread = threading.Thread(target=self.run_sequence, args=(reason,), name="shutdown", daemon=True)
            self._thread.start()
            return True

    def request_abort(self, reason: str) -> None:
        self._abort_reason = reason
        self._abort.set()

    def close(self, timeout: float = 5.0) -> None:
        if self.running:
            self.request_abort("monitor stopping")
            self._thread.join(timeout)

    def recover(self) -> None:
        store = _RunStore(self._open())
        try:
            count = store.recover(int(self._clock()))
        finally:
            store.close()
        if count:
            self._emit(
                int(self._clock()), "shutdown_interrupted", "warning",
                "A shutdown sequence was interrupted by a monitor restart; it will restart if the condition persists",
                {},
            )

    # -- sequence

    def _open(self) -> sqlite3.Connection | None:
        if self._conn_factory is None:
            return None
        try:
            return self._conn_factory()
        except sqlite3.Error:
            log.exception("shutdown thread could not open the database")
            return None

    def run_sequence(self, reason: str) -> None:
        store = _RunStore(self._open())
        try:
            self._sequence(store, reason)
        finally:
            store.close()

    def _sequence(self, store: _RunStore, reason: str) -> None:
        cfg = self.cfg
        names = [m.name for m in cfg.machines] + ([_SELF] if cfg.shutdown_self else [])
        dry = cfg.dry_run
        run_id = store.create_run(int(self._clock()), dry, reason, names)
        self._emit(
            int(self._clock()), "shutdown_started", "critical",
            f"{'DRY RUN: ' if dry else ''}Shutdown sequence started: {reason}. Order: {', '.join(names) or 'nothing'}",
            {"dry_run": dry, "order": names, "reason": reason},
        )

        for seq, machine in enumerate(cfg.machines):
            if self._abort.is_set():
                self._abort_run(store, run_id, seq, names)
                return
            self._run_machine(store, run_id, seq, machine)

        if cfg.shutdown_self:
            seq = len(cfg.machines)
            if self._abort.is_set():
                self._abort_run(store, run_id, seq, names)
                return
            self._run_self(store, run_id, seq)
        else:
            store.finish_run(run_id, "completed", int(self._clock()))
            self._emit(int(self._clock()), "shutdown_completed", "critical", "Shutdown sequence completed", {})

    def _abort_run(self, store: _RunStore, run_id: int | None, from_seq: int, names: list[str]) -> None:
        now = int(self._clock())
        for seq in range(from_seq, len(names)):
            store.step(run_id, seq, "skipped", now, f"aborted: {self._abort_reason}")
        store.finish_run(run_id, "aborted", now)
        done = names[:from_seq]
        self._emit(
            now, "shutdown_aborted", "warning",
            f"Shutdown sequence aborted ({self._abort_reason}). Already shut down, may need a manual restart: "
            f"{', '.join(done) or 'none'}",
            {"reason": self._abort_reason, "already_shut_down": done},
        )

    def _step_event(self, name: str, ok: bool, detail: str) -> None:
        self._emit(
            int(self._clock()), "shutdown_step", "info" if ok else "critical", f"{name}: {detail}",
            {"machine": name, "ok": ok},
        )

    def _run_machine(self, store: _RunStore, run_id: int | None, seq: int, m: MachineConfig) -> None:
        command = m.command or settings.POWEROFF_COMMAND
        store.step(run_id, seq, "running", int(self._clock()))

        if self.cfg.dry_run:
            detail = f"dry run, would run on {m.user}@{m.host}: {command}"
            store.step(run_id, seq, "done", int(self._clock()), detail)
            self._step_event(m.name, True, detail)
            return

        result = CommandResult(1)
        for attempt in range(1, settings.SSH_ATTEMPTS + 1):
            result = self._runner.ssh(m, command)
            # A poweroff can drop the SSH session before it returns, so a non-zero exit is judged by ping.
            if result.returncode == 0 or not self._runner.ping(m.host):
                break
            if attempt < settings.SSH_ATTEMPTS and self._wait(settings.SSH_RETRY_DELAY_S):
                detail = f"aborted before the command succeeded: {self._abort_reason}"
                store.step(run_id, seq, "skipped", int(self._clock()), detail)
                self._step_event(m.name, False, detail)
                return

        sent = result.returncode == 0
        if not sent and not self._runner.ping(m.host):
            detail = f"not answering ping, assumed already down (ssh exit {result.returncode})"
            store.step(run_id, seq, "skipped", int(self._clock()), detail)
            self._step_event(m.name, True, detail)
            return
        # Exit 255 is an ssh-level error: the session may simply have been cut by the host going down.
        if not sent and result.returncode != 255:
            detail = f"remote command failed (exit {result.returncode}): {result.output}"
            store.step(run_id, seq, "failed", int(self._clock()), detail)
            self._step_event(m.name, False, detail)
            return

        outcome = self._wait_until_down(m)
        if outcome == "timeout":
            detail = (
                f"{'poweroff sent' if sent else 'ssh failed (exit 255)'} but the host still answers ping "
                f"after {m.wait_for_down_s} s"
            )
            store.step(run_id, seq, "failed", int(self._clock()), detail)
            self._step_event(m.name, False, detail)
        else:
            detail = "host stopped answering ping" if outcome == "down" else (
                f"waiting stopped early ({self._abort_reason})"
            )
            if sent:
                detail = "poweroff sent, " + detail
            store.step(run_id, seq, "done", int(self._clock()), detail)
            self._step_event(m.name, True, detail)

    def _wait_until_down(self, m: MachineConfig) -> str:
        deadline = self._monotonic() + m.wait_for_down_s
        while self._runner.ping(m.host):
            if self._monotonic() >= deadline:
                return "timeout"
            if self._wait(settings.PING_INTERVAL_S):
                return "interrupted"
        return "down"

    def _run_self(self, store: _RunStore, run_id: int | None, seq: int) -> None:
        argv = shlex.split(settings.POWEROFF_COMMAND)
        now = int(self._clock())
        # Record everything first: once poweroff runs this process is killed.
        detail = f"dry run, would run: {settings.POWEROFF_COMMAND}" if self.cfg.dry_run else "poweroff issued"
        store.step(run_id, seq, "done", now, detail)
        store.finish_run(run_id, "completed", now)
        self._emit(
            now, "shutdown_completed", "critical",
            f"{'DRY RUN: ' if self.cfg.dry_run else ''}Shutdown sequence completed; "
            f"{'this host would now power off' if self.cfg.dry_run else 'powering off this host now'}",
            {"dry_run": self.cfg.dry_run},
        )
        if self.cfg.dry_run:
            return
        if self._before_poweroff is not None:
            try:
                self._before_poweroff()  # e.g. flush queued alert emails
            except Exception:
                log.exception("before-poweroff hook failed")
        result = self._runner.local(argv)
        if result.returncode != 0:
            failure = f"could not power off this host (exit {result.returncode}): {result.output}"
            store.step(run_id, seq, "failed", int(self._clock()), failure)
            self._step_event(_SELF, False, failure)


# ---------------------------------------------------------------- preflight


@dataclass(frozen=True)
class StepResult:
    ok: bool | None  # None: not applicable, not enabled, or skipped
    detail: str


@dataclass(frozen=True)
class TargetCheck:
    name: str
    position: int  # 1-based shutdown order, this host last
    ping: StepResult | None = None
    ssh: StepResult | None = None
    sudo: StepResult | None = None
    dry: StepResult | None = None
    remote_hostname: str | None = None

    @property
    def overall(self) -> str:
        """ready: everything verified; partial: required steps pass but the dry-run is not enabled/verifiable."""
        steps = (self.ping, self.ssh, self.sudo, self.dry)
        if any(st is not None and st.ok is False for st in (self.ping, self.ssh, self.sudo)):
            return "attention"
        if self.dry is not None and self.dry.ok is False:
            return "attention"
        if any(st is not None and st.ok is None for st in (self.sudo, self.dry)) or all(st is None for st in steps):
            return "partial"
        return "ready"

    @property
    def ok(self) -> bool:
        return self.overall != "attention"

    @property
    def detail(self) -> str:
        for label, st in (("ping", self.ping), ("ssh", self.ssh), ("sudo", self.sudo), ("dry-run", self.dry)):
            if st is not None and st.ok is False:
                return f"{label}: {st.detail}"
        parts = [f"{label} {'OK' if st.ok else st.detail}" for label, st in
                 (("ping", self.ping), ("ssh", self.ssh), ("sudo", self.sudo), ("dry-run", self.dry))
                 if st is not None]
        return ", ".join(parts)


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_SUDO_DENIED = ("a password is required", "is not allowed", "may not run sudo", "not in the sudoers")
_OPTION_UNKNOWN = ("unrecognized option", "unknown option", "invalid option")


def _clean_hostname(output: str) -> str | None:
    """Last plausible line of `hostname` output; ssh may print warnings before it."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if _HOSTNAME_RE.match(line):
            return line
    return None


def _scrub(text: str, m: MachineConfig) -> str:
    """Keep addresses out of anything shown on the dashboard or sent by email."""
    return text.replace(f"{m.user}@{m.host}", m.name).replace(m.host, m.name)


def _classify_dry(res: CommandResult, detail_prefix: str = "", local: bool = False) -> StepResult:
    if res.returncode == 0:
        return StepResult(True, "poweroff dry-run OK")
    text = res.output.lower()
    if any(w in text for w in _SUDO_DENIED):
        fix = "run sudo deploy/install.sh on this host" if local else "re-run setup-target.sh on that machine"
        return StepResult(None, f"not enabled: {fix} to allow the dry-run")
    if any(w in text for w in _OPTION_UNKNOWN):
        return StepResult(None, "not supported by this systemctl")
    return StepResult(False, f"{detail_prefix}failed (exit {res.returncode}): {res.output}")


def _check_machine(m: MachineConfig, position: int, runner: Runner, accept_new: bool) -> TargetCheck:
    ping = StepResult(True, "answers ping") if runner.ping(m.host) else StepResult(
        False, "does not answer ping (the shutdown wait relies on ping)")

    login = runner.ssh(m, settings.HOSTNAME_COMMAND, accept_new=accept_new)
    if login.returncode != 0:
        ssh = StepResult(False, _scrub(f"ssh login failed (exit {login.returncode}): {login.output}", m))
        skipped = StepResult(None, "skipped, ssh login failed")
        return TargetCheck(m.name, position, ping, ssh, skipped, skipped)
    ssh = StepResult(True, "login OK, host key matches")
    hostname = _clean_hostname(login.output)

    if m.command is not None:
        na = StepResult(None, "custom command: cannot be verified without running it")
        return TargetCheck(m.name, position, ping, ssh, na, na, hostname)

    sudo_res = runner.ssh(m, settings.SUDO_CHECK_COMMAND, accept_new=accept_new)
    if sudo_res.returncode == 0:
        sudo = StepResult(True, "sudo allows the poweroff command")
    else:
        sudo = StepResult(False, _scrub(f"sudo check failed (exit {sudo_res.returncode}): {sudo_res.output}", m))
    dry = _classify_dry(runner.ssh(m, settings.DRYRUN_COMMAND, accept_new=accept_new))
    if dry.ok is False:
        dry = StepResult(False, _scrub(dry.detail, m))
    return TargetCheck(m.name, position, ping, ssh, sudo, dry, hostname)


def preflight(cfg: ShutdownConfig, runner: Runner, accept_new: bool = False) -> list[TargetCheck]:
    """Exercise everything a real shutdown needs, short of powering anything off."""
    checks = [_check_machine(m, i, runner, accept_new) for i, m in enumerate(cfg.machines, start=1)]
    if cfg.shutdown_self:
        sudo_res = runner.local(shlex.split(settings.SUDO_CHECK_COMMAND))
        sudo = StepResult(True, "sudo allows the poweroff command") if sudo_res.returncode == 0 else StepResult(
            False, f"sudo check failed (exit {sudo_res.returncode}): {sudo_res.output}")
        dry = _classify_dry(runner.local(shlex.split(settings.DRYRUN_COMMAND)), local=True)
        checks.append(TargetCheck(_SELF, len(checks) + 1, None, None, sudo, dry))
    return checks
