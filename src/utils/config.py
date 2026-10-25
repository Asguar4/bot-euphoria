"""Lazy, shared JSON configuration."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from utils._paths import project_root

__all__ = ["Config", "ConfigError", "get_config_path", "set_config_path"]

_lock = threading.RLock()
_explicit_path: Path | None = None
_shared: Config | None = None


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or invalid."""


def get_config_path() -> Path:
    """Resolve the configuration file path.

    Order: explicit path, env ``EUPHORIA_CONFIG``, ``<project_root>/config.json``.

    Returns:
        Path of the configuration file.
    """
    if _explicit_path is not None:
        return _explicit_path
    env = os.environ.get("EUPHORIA_CONFIG")
    if env:
        return Path(env)
    return project_root() / "config.json"


def set_config_path(path: Path) -> None:
    """Set an explicit config path; reloads eagerly if already loaded.

    If the config is not loaded yet, only the path is stored (no I/O). Otherwise
    the new file is read first and the shared dict is updated in place.

    Args:
        path: Path to the config file.

    Raises:
        ConfigError: If the config is loaded and the new file cannot be read;
            the old path and content are kept.
    """
    global _explicit_path
    new_path = Path(path)
    with _lock:
        if _shared is not None and _shared._loaded:
            data = _read_json(new_path)
            _shared.config.clear()
            _shared.config.update(data)
        _explicit_path = new_path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in config {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must contain a JSON object")
    return data


class Config:
    """Singleton holder of the parsed configuration."""

    _instance: Config | None = None
    config: dict[str, Any]
    _loaded: bool

    def __new__(cls) -> Config:
        global _shared
        with _lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst.config = {}
                inst._loaded = False
                cls._instance = inst
                _shared = inst
            return cls._instance

    def __init__(self) -> None:
        with _lock:
            if not self._loaded:
                self.update_config()

    def update_config(self) -> None:
        """Re-read the file and update ``config`` in place.

        Raises:
            ConfigError: On a missing file or invalid JSON (old content is kept).
        """
        data = _read_json(get_config_path())
        with _lock:
            self.config.clear()
            self.config.update(data)
            self._loaded = True


def __getattr__(name: str) -> Any:
    if name == "cfg":
        return Config().config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
