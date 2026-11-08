"""SQLite access. The monitor is the only writer; the web app opens the file read-only."""

import json
import sqlite3
from pathlib import Path

from . import settings

# Append-only list; index + 1 is the schema version stored in PRAGMA user_version.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE samples (
        ts              INTEGER PRIMARY KEY,   -- epoch seconds, UTC
        status          TEXT NOT NULL,
        battery_charge  REAL,
        battery_voltage REAL,
        battery_runtime INTEGER,
        input_voltage   REAL,
        input_frequency REAL,
        output_voltage  REAL,
        output_current  REAL,
        output_frequency REAL,
        realpower       REAL,
        power_va        REAL,
        load_pct        REAL,
        temperature     REAL
    );

    CREATE TABLE samples_1m (
        ts                  INTEGER PRIMARY KEY,   -- start of minute, epoch seconds UTC
        n                   INTEGER NOT NULL,
        ob_count            INTEGER NOT NULL,
        battery_charge      REAL,
        min_battery_charge  REAL,
        battery_voltage     REAL,
        battery_runtime     REAL,
        min_battery_runtime INTEGER,
        input_voltage       REAL,
        input_frequency     REAL,
        output_voltage      REAL,
        output_current      REAL,
        output_frequency    REAL,
        realpower           REAL,
        power_va            REAL,
        load_pct            REAL,
        temperature         REAL
    );

    CREATE TABLE ups_info (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_ts INTEGER NOT NULL
    );

    CREATE TABLE events (
        id       INTEGER PRIMARY KEY,
        ts       INTEGER NOT NULL,
        type     TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
        message  TEXT NOT NULL,
        data     TEXT                          -- JSON
    );
    CREATE INDEX events_ts ON events (ts);

    CREATE TABLE shutdown_runs (
        id          INTEGER PRIMARY KEY,
        started_ts  INTEGER NOT NULL,
        finished_ts INTEGER,
        state       TEXT NOT NULL CHECK (state IN ('running', 'completed', 'aborted')),
        dry_run     INTEGER NOT NULL,
        reason      TEXT NOT NULL
    );

    CREATE TABLE shutdown_steps (
        id          INTEGER PRIMARY KEY,
        run_id      INTEGER NOT NULL REFERENCES shutdown_runs (id),
        seq         INTEGER NOT NULL,
        machine     TEXT NOT NULL,
        state       TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed', 'skipped')),
        started_ts  INTEGER,
        finished_ts INTEGER,
        detail      TEXT,
        UNIQUE (run_id, seq)
    );
    """,
    """
    -- Written every poll (even failed ones) so the web app can tell the monitor is alive.
    CREATE TABLE monitor_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_ts INTEGER NOT NULL
    );
    """,
    """
    -- Latest shutdown-readiness result per machine ("this host" included); names only, no addresses.
    CREATE TABLE readiness (
        name            TEXT PRIMARY KEY,
        position        INTEGER NOT NULL,
        remote_hostname TEXT,
        ping_ok         INTEGER, ping_detail TEXT,
        ssh_ok          INTEGER, ssh_detail  TEXT,
        sudo_ok         INTEGER, sudo_detail TEXT,
        dry_ok          INTEGER, dry_detail  TEXT,
        overall         TEXT NOT NULL,
        checked_ts      INTEGER NOT NULL,
        trigger         TEXT NOT NULL
    );
    """,
]


def connect(path: Path = settings.DB_PATH, *, shared: bool = False) -> sqlite3.Connection:
    # shared=True lets several threads use one connection; callers must serialise with a lock.
    conn = sqlite3.connect(path, timeout=settings.DB_BUSY_TIMEOUT_MS / 1000, check_same_thread=not shared)
    conn.row_factory = sqlite3.Row
    # Must precede journal_mode: it only takes effect while the database is still empty.
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_readonly(path: Path = settings.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=settings.DB_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(MIGRATIONS)):
        conn.executescript(f"BEGIN;\n{MIGRATIONS[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    return len(MIGRATIONS)


def add_event(
    conn: sqlite3.Connection,
    ts: int,
    type_: str,
    severity: str,
    message: str,
    data: dict | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO events (ts, type, severity, message, data) VALUES (?, ?, ?, ?, ?)",
            (ts, type_, severity, message, json.dumps(data) if data is not None else None),
        )


def init_db(path: Path = settings.DB_PATH) -> int:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        return migrate(conn)
