"""Typed application settings built from the raw config mapping."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils._paths import project_root
from utils.config import ConfigError
from utils.database import DatabaseSettings

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _default_state_file() -> Path:
    return project_root() / ".run" / "control.json"


def _default_log_dir() -> Path:
    return project_root() / "logs"


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Discord bot settings."""

    token: str = field(repr=False)
    extensions: tuple[str, ...] = ()
    shutdown_timeout: float = 30.0
    timezone: str = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class ControlSettings:
    """Control server settings."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 48600
    token: str | None = field(default=None, repr=False)
    allow_remote: bool = False
    state_file: Path = field(default_factory=_default_state_file)
    max_connections: int = 8
    idle_timeout: float = 60.0
    command_timeout: float = 120.0


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """Logging settings."""

    dir: Path = field(default_factory=_default_log_dir)
    level: str = "INFO"
    console: bool = True


@dataclass(frozen=True, slots=True)
class AppSettings:
    """All application settings."""

    bot: BotSettings
    control: ControlSettings
    logging: LoggingSettings
    database: DatabaseSettings


@dataclass(frozen=True, slots=True)
class CliOverrides:
    """Command line overrides of config values."""

    log_dir: Path | None = None
    control_host: str | None = None
    control_port: int | None = None
    control_enabled: bool | None = None
    console_log: bool | None = None


def is_loopback(host: str) -> bool:
    """Return True if ``host`` is a loopback address or ``localhost``."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _section(raw: Mapping[str, Any], name: str, *, required: bool = False) -> Mapping[str, Any]:
    value = raw.get(name)
    if value is None:
        if required:
            raise ConfigError(f"{name}: missing required section")
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name}: expected an object")
    return value


def _typed(sec: Mapping[str, Any], path: str, key: str, kind: type, default: Any) -> Any:
    value = sec.get(key)
    if value is None:
        return default
    if kind is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    else:
        ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    if not ok:
        raise ConfigError(f"{path}.{key}: expected {kind.__name__}, got {value!r}")
    return value


def _resolve(value: Any, base_dir: Path, path: str) -> Path:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: expected a string path")
    p = Path(value)
    return p if p.is_absolute() else base_dir / p


def load_settings(
    raw: Mapping[str, Any], *, base_dir: Path, overrides: CliOverrides | None = None
) -> AppSettings:
    """Validate the raw config and build :class:`AppSettings` (pure).

    Args:
        raw: Parsed config JSON.
        base_dir: Directory against which relative paths are resolved.
        overrides: Optional CLI overrides.

    Raises:
        ConfigError: With the offending key path.
    """
    ov = overrides or CliOverrides()

    b = _section(raw, "bot", required=True)
    token = b.get("TOKEN")
    if not isinstance(token, str) or not token:
        raise ConfigError("bot.TOKEN: required non-empty string")
    exts = b.get("EXTENSIONS") or []
    if not isinstance(exts, list) or not all(isinstance(e, str) for e in exts):
        raise ConfigError("bot.EXTENSIONS: expected a list of strings")
    shutdown_timeout = float(_typed(b, "bot", "SHUTDOWN_TIMEOUT", float, 30.0))
    if shutdown_timeout <= 0:
        raise ConfigError("bot.SHUTDOWN_TIMEOUT: must be > 0")
    bot = BotSettings(
        token=token,
        extensions=tuple(exts),
        shutdown_timeout=shutdown_timeout,
        timezone=_typed(b, "bot", "TIMEZONE", str, "Europe/Moscow"),
    )

    c = _section(raw, "control")
    host = ov.control_host or _typed(c, "control", "HOST", str, "127.0.0.1")
    port = (
        ov.control_port if ov.control_port is not None else _typed(c, "control", "PORT", int, 48600)
    )
    if not 0 <= port <= 65535:
        raise ConfigError("control.PORT: must be in 0..65535")
    allow_remote = c.get("ALLOW_REMOTE", False)
    if not isinstance(allow_remote, bool):
        raise ConfigError("control.ALLOW_REMOTE: expected a boolean")
    ctl_token = _typed(c, "control", "TOKEN", str, None)
    enabled = _typed(c, "control", "ENABLED", bool, True)
    if ov.control_enabled is not None:
        enabled = ov.control_enabled
    if not is_loopback(host):
        if not allow_remote:
            raise ConfigError(f"control.HOST: {host!r} is not loopback; set ALLOW_REMOTE to true")
        if ctl_token is None or len(ctl_token) < 16:
            raise ConfigError("control.TOKEN: ALLOW_REMOTE requires a token of >= 16 characters")
    state_file = c.get("STATE_FILE")
    max_connections = _typed(c, "control", "MAX_CONNECTIONS", int, 8)
    idle_timeout = float(_typed(c, "control", "IDLE_TIMEOUT", float, 60.0))
    command_timeout = float(_typed(c, "control", "COMMAND_TIMEOUT", float, 120.0))
    for key, value in (
        ("MAX_CONNECTIONS", max_connections),
        ("IDLE_TIMEOUT", idle_timeout),
        ("COMMAND_TIMEOUT", command_timeout),
    ):
        if value <= 0:
            raise ConfigError(f"control.{key}: must be > 0")
    control = ControlSettings(
        enabled=enabled,
        host=host,
        port=port,
        token=ctl_token,
        allow_remote=allow_remote,
        state_file=(
            _resolve(state_file, base_dir, "control.STATE_FILE")
            if state_file is not None
            else _default_state_file()
        ),
        max_connections=max_connections,
        idle_timeout=idle_timeout,
        command_timeout=command_timeout,
    )

    lg = _section(raw, "logging")
    level = str(_typed(lg, "logging", "LEVEL", str, "INFO")).upper()
    if level not in _LEVELS:
        raise ConfigError(f"logging.LEVEL: must be one of {'|'.join(_LEVELS)}")
    log_dir = lg.get("DIR")
    logging_settings = LoggingSettings(
        dir=ov.log_dir
        or (
            _resolve(log_dir, base_dir, "logging.DIR")
            if log_dir is not None
            else _default_log_dir()
        ),
        level=level,
        console=(
            ov.console_log
            if ov.console_log is not None
            else _typed(lg, "logging", "CONSOLE", bool, True)
        ),
    )

    database = DatabaseSettings.from_mapping(_section(raw, "database", required=True))
    return AppSettings(bot=bot, control=control, logging=logging_settings, database=database)
