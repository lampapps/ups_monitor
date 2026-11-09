"""Application-wide constants. Tunable values belong in config.yaml, secrets in .env."""

from pathlib import Path

from dotenv import load_dotenv

VERSION = "0.1.1"

# Everything the app owns lives under BASE_DIR, so the tree can be moved.
BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ups.db"
KEYS_DIR = BASE_DIR / "keys"
SSH_KEY_PATH = KEYS_DIR / "ups_shutdown"
KNOWN_HOSTS_PATH = KEYS_DIR / "known_hosts"
STATIC_DIR = BASE_DIR / "app" / "web" / "static"

DISPLAY_TIMEZONE = "America/New_York"

# NUT variable -> samples column. Order here is the column order used by the poller.
SAMPLE_FIELDS = {
    "ups.status": "status",
    "battery.charge": "battery_charge",
    "battery.voltage": "battery_voltage",
    "battery.runtime": "battery_runtime",
    "input.voltage": "input_voltage",
    "input.frequency": "input_frequency",
    "output.voltage": "output_voltage",
    "output.current": "output_current",
    "output.frequency": "output_frequency",
    "ups.realpower": "realpower",
    "ups.power": "power_va",
    "ups.load": "load_pct",
    "ups.temperature": "temperature",
}

# Slow-changing values shown in the UI header, stored as key/value in ups_info.
INFO_FIELDS = (
    "ups.mfr",
    "ups.model",
    "ups.serial",
    "ups.type",
    "ups.test.result",
    "ups.test.date",
    "ups.test.interval",
    "battery.low",
    "input.voltage.nominal",
    "input.transfer.high",
    "input.transfer.low",
    "ups.realpower.nominal",
    "ups.power.nominal",
)

# Defaults for timeouts the owner should not need to tune.
SSH_CONNECT_TIMEOUT_S = 5
DB_BUSY_TIMEOUT_MS = 5000
# Failed polls must last this long before a "UPS unreachable" event is raised.
UNREACHABLE_AFTER_S = 30

# Shutdown. The target's sudoers entry should allow exactly this command and nothing else.
POWEROFF_COMMAND = "sudo -n /usr/bin/systemctl poweroff"
SUDO_CHECK_COMMAND = "sudo -n -l /usr/bin/systemctl poweroff"
SSH_ATTEMPTS = 2
SSH_RETRY_DELAY_S = 3
SSH_COMMAND_TIMEOUT_S = 30
PING_INTERVAL_S = 2
PREFLIGHT_FIRST_DELAY_S = 60
# Closest safe stand-in for the real poweroff: systemctl prints what it would do and stops.
DRYRUN_COMMAND = "sudo -n /usr/bin/systemctl poweroff --dry-run"
HOSTNAME_COMMAND = "hostname"

# "Check now": the web service drops this file, the monitor consumes it and runs the readiness check.
CHECK_REQUEST_PATH = DATA_DIR / "check-now.request"
CHECK_COOLDOWN_S = 30
SCHEDULER_POLL_S = 2

# Email: every event the owner can switch on or off under smtp.events, with its default.
# self_test and shutdown_step are split by outcome. notify_failed is never mailed (it would loop).
NOTIFY_EVENT_DEFAULTS = {
    "on_battery": True,
    "power_restored": True,
    "low_battery": True,
    "replace_battery": True,
    "overload": True,
    "self_test_ok": False,
    "self_test_failed": True,
    "ups_unreachable": True,
    "ups_recovered": True,
    "shutdown_started": True,
    "shutdown_step_ok": False,
    "shutdown_step_failed": True,
    "shutdown_completed": True,
    "shutdown_aborted": True,
    "shutdown_interrupted": True,
    "preflight_failed": True,
    "preflight_ok": True,
    "monitor_started": False,
    "monitor_stopped": False,
    "conn_refused": False,
    "retention_failed": True,
}
NOTIFY_NEVER_TYPES = frozenset({"notify_failed"})

# Retention: hourly rollup of raw samples into 1-minute averages, then purge by age.
RETENTION_INTERVAL_S = 3600
RETENTION_FIRST_DELAY_S = 30
ROLLUP_CHUNK_S = 86400  # one transaction per day of data, so the poller is never blocked for long
PURGE_BATCH_ROWS = 20000
VACUUM_PAGES_PER_STEP = 2000
VACUUM_MAX_STEPS = 200
# A database created before auto_vacuum was enabled is converted once, but only if it is small.
LEGACY_VACUUM_MAX_BYTES = 64 * 1024 * 1024
NOTIFY_QUEUE_MAX = 100
NOTIFY_RETRY_DELAYS_S = (5, 30)
# Upper bound on waiting for queued mail just before this host powers off.
NOTIFY_FLUSH_TIMEOUT_S = 30

KUMA_TIMEOUT_S = 5
KUMA_STALE_MIN_S = 30

# Web UI: the monitor counts as stopped when its last poll heartbeat is older than this (and 3 poll intervals).
HEALTH_STALE_MIN_S = 30
WEB_MAX_POINTS = 600
WEB_EVENTS_MAX = 500


def load_env(path: Path = ENV_PATH) -> None:
    """Load .env into os.environ without overriding variables already set (e.g. by systemd)."""
    load_dotenv(path, override=False)
