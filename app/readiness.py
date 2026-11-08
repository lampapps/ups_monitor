"""Latest shutdown-readiness results and check-run state, stored for the web UI."""

import json
import sqlite3
from collections.abc import Iterable

from .shutdown import StepResult, TargetCheck

_RUN_KEY = "readiness_run"
_STEPS = ("ping", "ssh", "sudo", "dry")


def _flag(step: StepResult | None) -> int | None:
    return None if step is None or step.ok is None else int(step.ok)


def _delete_except(conn: sqlite3.Connection, names: list[str]) -> None:
    if not names:  # NOT IN (NULL) would match nothing
        conn.execute("DELETE FROM readiness")
        return
    conn.execute(f"DELETE FROM readiness WHERE name NOT IN ({','.join('?' * len(names))})", names)


def save(conn: sqlite3.Connection, checks: Iterable[TargetCheck], ts: int, trigger: str) -> None:
    checks = list(checks)
    with conn:
        _delete_except(conn, [c.name for c in checks])
        for c in checks:
            values: list = [c.name, c.position, c.remote_hostname]
            for step in _STEPS:
                st = getattr(c, step)
                values += [_flag(st), None if st is None else st.detail]
            values += [c.overall, ts, trigger]
            conn.execute(
                "INSERT OR REPLACE INTO readiness (name, position, remote_hostname, ping_ok, ping_detail, "
                "ssh_ok, ssh_detail, sudo_ok, sudo_detail, dry_ok, dry_detail, overall, checked_ts, trigger) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )


def prune(conn: sqlite3.Connection, names: list[str]) -> None:
    """Drop results for machines that are no longer configured."""
    with conn:
        _delete_except(conn, names)


def load(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM readiness ORDER BY position").fetchall()


def get_run(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM monitor_state WHERE key = ?", (_RUN_KEY,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except ValueError:
        state = {}
    return {
        "state": state.get("state", "idle"),
        "trigger": state.get("trigger"),
        "started_ts": state.get("started_ts"),
        "finished_ts": state.get("finished_ts"),
        "note": state.get("note"),
    }


def set_run(
    conn: sqlite3.Connection,
    ts: int,
    state: str,
    trigger: str | None = None,
    started_ts: int | None = None,
    finished_ts: int | None = None,
    note: str | None = None,
) -> None:
    value = json.dumps(
        {"state": state, "trigger": trigger, "started_ts": started_ts, "finished_ts": finished_ts, "note": note}
    )
    with conn:
        conn.execute(
            "INSERT INTO monitor_state (key, value, updated_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
            (_RUN_KEY, value, ts),
        )
