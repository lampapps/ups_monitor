"""Read-only queries for the web UI."""

import json
import math
import sqlite3
from pathlib import Path

from .. import readiness as readiness_store
from .. import settings
from ..config import ShutdownConfig

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}


_OB_RAW = "MAX(instr(' ' || status || ' ', ' OB ') > 0)"

# series name -> (raw SQL expression, rollup SQL expression, scale)
_SERIES = {
    "charge": ("AVG(battery_charge)", "AVG(battery_charge)", 1),
    "runtime_min": ("MIN(battery_runtime)", "MIN(min_battery_runtime)", 1 / 60),
    "battery_v": ("AVG(battery_voltage)", "AVG(battery_voltage)", 1),
    "input_v": ("AVG(input_voltage)", "AVG(input_voltage)", 1),
    "output_v": ("AVG(output_voltage)", "AVG(output_voltage)", 1),
    "load_pct": ("AVG(load_pct)", "AVG(load_pct)", 1),
    "realpower": ("AVG(realpower)", "AVG(realpower)", 1),
    "power_va": ("AVG(power_va)", "AVG(power_va)", 1),
    "temperature": ("AVG(temperature)", "AVG(temperature)", 1),
}


def _num(value, scale: float = 1, digits: int = 2):
    return None if value is None else round(value * scale, digits)


def history(conn: sqlite3.Connection, range_key: str, now: int, raw_max_s: int) -> dict:
    """Ranges up to raw_max_s (the raw retention window) use raw samples, longer ones the 1-minute rollups."""
    seconds = RANGES[range_key]
    raw = seconds <= raw_max_s
    base = 1 if raw else 60
    bucket = max(base, math.ceil(seconds / settings.WEB_MAX_POINTS))
    if not raw:
        bucket = math.ceil(bucket / 60) * 60
    cols = ", ".join(f"{(r if raw else m)} AS {name}" for name, (r, m, _) in _SERIES.items())
    ob = _OB_RAW if raw else "MAX(ob_count > 0)"
    table = "samples" if raw else "samples_1m"
    rows = conn.execute(
        f"SELECT (ts / :b) * :b AS t, {cols}, {ob} AS ob FROM {table} "
        "WHERE ts >= :start AND ts <= :end GROUP BY t ORDER BY t",
        {"b": bucket, "start": now - seconds, "end": now},
    ).fetchall()
    out: dict = {"range": range_key, "bucket_s": bucket, "start": now - seconds, "end": now, "t": [r["t"] for r in rows]}
    for name, (_, _, scale) in _SERIES.items():
        out[name] = [_num(r[name], scale) for r in rows]
    out["ob"] = [bool(r["ob"]) for r in rows]
    return out


def latest_sample(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def ups_info(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM ups_info")}


def last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value, updated_ts FROM monitor_state WHERE key = 'last_poll'").fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row["value"])
    except ValueError:
        state = {}
    return {"ts": row["updated_ts"], "ok": bool(state.get("ok")), "error_kind": state.get("error_kind")}


def events_total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def events(conn: sqlite3.Connection, limit: int, offset: int = 0) -> list[dict]:
    """Newest first."""
    result = []
    rows = conn.execute(
        "SELECT id, ts, type, severity, message, data FROM events ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else None
        except ValueError:
            data = None
        result.append(
            {"id": r["id"], "ts": r["ts"], "type": r["type"], "severity": r["severity"],
             "message": r["message"], "data": data}
        )
    return result


def shutdown_runs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    runs = []
    for r in conn.execute(
        "SELECT id, started_ts, finished_ts, state, dry_run, reason FROM shutdown_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall():
        steps = conn.execute(
            "SELECT seq, machine, state, started_ts, finished_ts, detail FROM shutdown_steps "
            "WHERE run_id = ? ORDER BY seq",
            (r["id"],),
        ).fetchall()
        runs.append({**dict(r), "dry_run": bool(r["dry_run"]), "steps": [dict(s) for s in steps]})
    return runs


_STEP_COLUMNS = ("ping", "ssh", "sudo", "dry")
# A verification older than this many check intervals no longer counts as verified.
STALE_AFTER_INTERVALS = 2


def _steps(row: sqlite3.Row) -> dict:
    steps = {}
    for step in _STEP_COLUMNS:
        ok, detail = row[f"{step}_ok"], row[f"{step}_detail"]
        steps[step] = None if detail is None else {"ok": None if ok is None else bool(ok), "detail": detail}
    return steps


def readiness_view(
    conn: sqlite3.Connection, cfg: ShutdownConfig, now: int, request_path: Path, monitor_alive: bool
) -> dict:
    """Configured machines in shutdown order, each with its latest readiness result (if any)."""
    rows = {r["name"]: r for r in readiness_store.load(conn)}
    run = readiness_store.get_run(conn)
    interval_s = cfg.preflight_interval_hours * 3600
    names = [m.name for m in cfg.machines] + (["this host"] if cfg.shutdown_self else [])

    targets = []
    for position, name in enumerate(names, start=1):
        row = rows.get(name)
        if row is None:
            targets.append({"position": position, "name": name, "remote_hostname": None, "overall": "unverified",
                            "steps": {s: None for s in _STEP_COLUMNS}, "checked_ts": None, "stale": False})
            continue
        stale = interval_s > 0 and now - row["checked_ts"] > STALE_AFTER_INTERVALS * interval_s
        overall = row["overall"]
        if stale and overall != "attention":
            overall = "unverified"
        targets.append({"position": position, "name": name, "remote_hostname": row["remote_hostname"],
                        "overall": overall, "steps": _steps(row), "checked_ts": row["checked_ts"], "stale": stale})

    if run["state"] == "running" and monitor_alive:
        state = "running"
    elif request_path.exists():
        state = "pending"
    else:
        state = "idle"
    return {
        "now": now,
        "state": state,
        "trigger": run["trigger"],
        "started_ts": run["started_ts"],
        "finished_ts": run["finished_ts"],
        "note": run["note"],
        "checked_ts": max((r["checked_ts"] for r in rows.values()), default=None),
        "interval_hours": cfg.preflight_interval_hours,
        "shutdown_enabled": cfg.enabled,
        "monitor_alive": monitor_alive,
        "targets": targets,
    }
