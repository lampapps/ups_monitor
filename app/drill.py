"""Rehearse the shutdown sequence without powering anything off: python -m app drill."""

import logging
from collections.abc import Mapping
from pathlib import Path

from . import db, settings
from .config import Config
from .events import EventDetector
from .notify import Notifier
from .shutdown import Runner, ShutdownEngine, SubprocessRunner

log = logging.getLogger(__name__)


def run_drill(
    cfg: Config,
    env: Mapping[str, str],
    db_path: Path = settings.DB_PATH,
    runner: Runner | None = None,
    flush_timeout: float = 15.0,
) -> list[tuple[str, str, str | None]]:
    """Run the whole sequence once in forced dry-run mode. Events, emails and DB rows are real; no host is touched."""
    db.init_db(db_path)
    event_conn = db.connect(db_path, shared=True)
    detector = EventDetector(event_conn)
    notifier = Notifier(cfg.smtp, env, detector.emit)
    notifier.start()
    detector.on_event.append(notifier.submit)
    # dry_run is forced so a drill can never reach the runner, whatever config.yaml says.
    engine = ShutdownEngine(
        cfg.shutdown.model_copy(update={"dry_run": True}),
        runner or SubprocessRunner(),
        detector.emit,
        conn_factory=lambda: db.connect(db_path),
    )
    try:
        engine.run_sequence("drill: manual rehearsal of the shutdown sequence (dry run)")
        notifier.flush(flush_timeout)
    finally:
        notifier.stop()
        event_conn.close()

    conn = db.connect(db_path)
    try:
        run = conn.execute("SELECT id FROM shutdown_runs ORDER BY id DESC LIMIT 1").fetchone()
        steps = conn.execute(
            "SELECT machine, state, detail FROM shutdown_steps WHERE run_id = ? ORDER BY seq", (run["id"],)
        ).fetchall()
        return [(s["machine"], s["state"], s["detail"]) for s in steps]
    finally:
        conn.close()
