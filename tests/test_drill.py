import pytest

from app import db, notify
from app.config import Config, MachineConfig, ShutdownConfig, SmtpConfig, UpsConfig
from app.drill import run_drill
from tests.test_notify import FakeSMTP
from tests.test_shutdown import ForbiddenRunner


def make_cfg(smtp=None):
    return Config(
        ups=UpsConfig(host="x"),
        shutdown=ShutdownConfig(
            dry_run=False,  # a drill must stay a dry run even when the config says live
            machines=[MachineConfig(name="a", host="a.lan", user="u"), MachineConfig(name="b", host="b.lan", user="u")],
        ),
        smtp=smtp or SmtpConfig(),
    )


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ups.db"


def rows(db_path, sql):
    conn = db.connect(db_path)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_drill_never_touches_a_machine_even_when_config_is_live(db_path):
    steps = run_drill(make_cfg(), {}, db_path, runner=ForbiddenRunner())
    assert [(m, s) for m, s, _ in steps] == [("a", "done"), ("b", "done"), ("this host", "done")]
    assert all("dry run" in detail for _, _, detail in steps)


def test_drill_is_recorded_as_a_dry_run(db_path):
    run_drill(make_cfg(), {}, db_path, runner=ForbiddenRunner())
    assert rows(db_path, "SELECT state, dry_run, reason FROM shutdown_runs")[0][:2] == ("completed", 1)
    types = [r[0] for r in rows(db_path, "SELECT type FROM events ORDER BY id")]
    assert types == ["shutdown_started", "shutdown_step", "shutdown_step", "shutdown_completed"]
    assert "DRY RUN" in rows(db_path, "SELECT message FROM events ORDER BY id")[0][0]


def test_drill_sends_the_configured_emails(db_path, monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    smtp = SmtpConfig(enabled=True, host="smtp.test", sender="ups@test", recipients=["a@test"], security="none")
    run_drill(make_cfg(smtp), {}, db_path, runner=ForbiddenRunner())
    assert len(FakeSMTP.instances) == 2  # started and completed; successful steps are off by default
    assert all("send" in s.calls for s in FakeSMTP.instances)
