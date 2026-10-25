"""Singleton registry of named loggers."""

from __future__ import annotations

import threading
from pathlib import Path

from utils._paths import project_root

from .logger import Logger
from .logSink import ConsoleSink, FileSink

_lock = threading.RLock()


class LoggerManager:
    """Thread-safe singleton creating loggers with file (and console) sinks."""

    _instance: LoggerManager | None = None
    _initialized = False

    def __new__(cls) -> LoggerManager:
        with _lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self) -> None:
        with _lock:
            if not LoggerManager._initialized:
                self.loggers: dict[str, Logger] = {}
                self._log_dir: Path = project_root() / "logs"
                self._console = False
                LoggerManager._initialized = True

    @property
    def log_dir(self) -> Path:
        """Directory where log files are written."""
        return self._log_dir

    def configure(self, log_dir: Path | None = None, *, console: bool | None = None) -> None:
        """Set log directory / console output for loggers created afterwards."""
        with _lock:
            if log_dir is not None:
                self._log_dir = Path(log_dir)
            if console is not None:
                self._console = console

    def create_logger(self, name: str) -> Logger:
        """Create (or return) the logger ``name``."""
        with _lock:
            if name not in self.loggers:
                logger = Logger()
                if self._console:
                    logger.sinks.append(ConsoleSink())
                logger.sinks.append(FileSink(self._log_dir / f"{name}.log"))
                self.loggers[name] = logger
            return self.loggers[name]

    def get_logger(self, name: str) -> Logger:
        """Return the logger ``name``, creating it if needed."""
        return self.create_logger(name)

    def remove_logger(self, name: str) -> None:
        """Remove a logger and close its sinks."""
        with _lock:
            logger = self.loggers.pop(name, None)
        if logger is not None:
            for sink in logger.sinks:
                sink.close()

    def shutdown(self) -> None:
        """Close all sinks of all loggers (idempotent)."""
        with _lock:
            loggers = list(self.loggers.values())
            self.loggers.clear()
        for logger in loggers:
            for sink in logger.sinks:
                try:
                    sink.close()
                except Exception:
                    continue
