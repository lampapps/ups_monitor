"""Load and validate config.yaml. Secrets are referenced by env var name, never stored here."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import settings


class ConfigError(Exception):
    pass


class _Model(BaseModel):
    # Forbid unknown keys so a typo in config.yaml fails loudly.
    model_config = ConfigDict(extra="forbid")


class UpsConfig(_Model):
    host: str
    port: int = Field(3493, ge=1, le=65535)
    name: str = "ups1"
    tls: bool = True
    connect_timeout_s: float = Field(3.0, gt=0)
    poll_interval_s: float = Field(5.0, ge=1)
    poll_interval_on_battery_s: float = Field(1.0, ge=1)


class MachineConfig(_Model):
    name: str
    host: str
    user: str
    port: int = Field(22, ge=1, le=65535)
    # Seconds to wait for the host to stop answering ping before moving on.
    wait_for_down_s: int = Field(120, ge=1)
    # Replaces the default `sudo -n /usr/bin/systemctl poweroff`.
    command: str | None = None


class ShutdownConfig(_Model):
    enabled: bool = True
    # Safe default: log and email what would happen, run nothing.
    dry_run: bool = True
    runtime_threshold_minutes: float = Field(10, gt=0)
    consecutive_polls: int = Field(3, ge=1)
    shutdown_self: bool = True
    # Hours between automatic ssh/sudo checks of every target (0 disables).
    preflight_interval_hours: float = Field(24, ge=0)
    # Order of this list is the shutdown order.
    machines: list[MachineConfig] = []

    @model_validator(mode="after")
    def _unique_names(self) -> "ShutdownConfig":
        names = [m.name for m in self.machines]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate machine names: {sorted(dupes)}")
        return self


class SmtpConfig(_Model):
    enabled: bool = False
    host: str = ""
    port: int = Field(587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    sender: str = ""
    recipients: list[str] = []
    username_env: str | None = None
    password_env: str | None = None
    timeout_s: float = Field(10.0, gt=0)
    # Overrides of settings.NOTIFY_EVENT_DEFAULTS: event name -> send or not.
    events: dict[str, bool] = {}

    @model_validator(mode="after")
    def _required_when_enabled(self) -> "SmtpConfig":
        unknown = sorted(set(self.events) - set(settings.NOTIFY_EVENT_DEFAULTS))
        if unknown:
            raise ValueError(
                f"unknown smtp.events {unknown}; valid names: {', '.join(settings.NOTIFY_EVENT_DEFAULTS)}"
            )
        if self.enabled and not (self.host and self.sender and self.recipients):
            raise ValueError("smtp.host, smtp.sender and smtp.recipients are required when enabled")
        return self

    def event_enabled(self, key: str) -> bool:
        return self.events.get(key, settings.NOTIFY_EVENT_DEFAULTS.get(key, False))


class KumaConfig(_Model):
    enabled: bool = False
    # Name of the env var holding the full push URL (it contains the token).
    push_url_env: str = "KUMA_PUSH_URL"
    interval_s: float = Field(60.0, ge=5)


class WebConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1, le=65535)
    # The "Check now" button is the only action the web UI can perform.
    allow_check_now: bool = True


class RetentionConfig(_Model):
    raw_days: int = Field(30, ge=1)
    rollup_days: int = Field(365, ge=1)
    events_days: int = Field(365, ge=1)
    shutdown_days: int = Field(365, ge=1)

    @model_validator(mode="after")
    def _rollups_outlive_raw(self) -> "RetentionConfig":
        if self.rollup_days < self.raw_days:
            raise ValueError("rollup_days must be at least raw_days")
        return self


class Config(_Model):
    ups: UpsConfig
    shutdown: ShutdownConfig = ShutdownConfig()
    smtp: SmtpConfig = SmtpConfig()
    kuma: KumaConfig = KumaConfig()
    web: WebConfig = WebConfig()
    retention: RetentionConfig = RetentionConfig()

    def required_env(self) -> list[str]:
        names: list[str] = []
        if self.smtp.enabled:
            names += [n for n in (self.smtp.username_env, self.smtp.password_env) if n]
        if self.kuma.enabled:
            names.append(self.kuma.push_url_env)
        return names


def load_config(
    path: Path = settings.CONFIG_PATH, env: Mapping[str, str] | None = None, check_env: bool = True
) -> Config:
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; run setup.sh or copy config.example.yaml") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    try:
        cfg = Config.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"invalid {path}:\n" + "\n".join(lines)) from exc

    missing = [n for n in cfg.required_env() if not env.get(n)] if check_env else []
    if missing:
        raise ConfigError(f"missing environment variables (set them in .env): {', '.join(missing)}")
    return cfg
