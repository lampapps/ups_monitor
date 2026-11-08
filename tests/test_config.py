from pathlib import Path

import pytest

from app import settings
from app.config import ConfigError, load_config

EXAMPLE = Path(settings.BASE_DIR) / "config.example.yaml"


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.shutdown.dry_run is True
    assert [m.name for m in cfg.shutdown.machines] == ["P3Tiny", "pi4-1"]


def test_minimal_config_uses_defaults(tmp_path):
    cfg = load_config(write(tmp_path, "ups:\n  host: 1.2.3.4\n"), env={})
    assert cfg.ups.port == 3493
    assert cfg.shutdown.dry_run is True
    assert cfg.shutdown.machines == []


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="ups.hots"):
        load_config(write(tmp_path, "ups:\n  host: x\n  hots: y\n"), env={})


def test_duplicate_machine_names_rejected(tmp_path):
    text = (
        "ups:\n  host: x\nshutdown:\n  machines:\n"
        "    - {name: a, host: h1, user: u}\n    - {name: a, host: h2, user: u}\n"
    )
    with pytest.raises(ConfigError, match="duplicate machine names"):
        load_config(write(tmp_path, text), env={})


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_enabled_smtp_requires_env_vars(tmp_path):
    text = (
        "ups:\n  host: x\nsmtp:\n  enabled: true\n  host: h\n  sender: a@b\n"
        "  recipients: [c@d]\n  password_env: SMTP_PASSWORD\n"
    )
    path = write(tmp_path, text)
    with pytest.raises(ConfigError, match="SMTP_PASSWORD"):
        load_config(path, env={})
    assert load_config(path, env={"SMTP_PASSWORD": "x"}).smtp.enabled


def test_disabled_features_need_no_env(tmp_path):
    load_config(write(tmp_path, "ups:\n  host: x\nkuma:\n  enabled: false\n"), env={})


def test_bad_values_rejected(tmp_path):
    text = "ups:\n  host: x\nshutdown:\n  runtime_threshold_minutes: 0\n"
    with pytest.raises(ConfigError, match="runtime_threshold_minutes"):
        load_config(write(tmp_path, text), env={})


def test_check_now_button_defaults_on_and_can_be_disabled(tmp_path):
    assert load_config(write(tmp_path, "ups:\n  host: x\n"), env={}).web.allow_check_now is True
    off = load_config(write(tmp_path, "ups:\n  host: x\nweb:\n  allow_check_now: false\n"), env={})
    assert off.web.allow_check_now is False
