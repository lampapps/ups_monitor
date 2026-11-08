import sqlite3

import pytest

from app import db


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "ups.db"


def test_init_creates_schema(db_path):
    assert db.init_db(db_path) == len(db.MIGRATIONS)
    with db.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"samples", "samples_1m", "ups_info", "events", "shutdown_runs", "shutdown_steps"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_is_idempotent(db_path):
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO events (ts, type, severity, message) VALUES (1, 't', 'info', 'm')")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_readonly_connection_rejects_writes(db_path):
    db.init_db(db_path)
    conn = db.connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO events (ts, type, severity, message) VALUES (1, 't', 'info', 'm')")


def test_severity_constraint(db_path):
    db.init_db(db_path)
    with db.connect(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO events (ts, type, severity, message) VALUES (1, 't', 'bad', 'm')")
