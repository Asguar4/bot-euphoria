"""Log sinks backed by stdlib logging handlers."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "EXCEPTION": logging.ERROR,
}

_FORMAT = "[%(asctime)s] [%(levelname)-7s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _make_record(level: str, message: str, name: str) -> logging.LogRecord:
    exc_info = None
    if level == "EXCEPTION" and sys.exc_info()[0] is not None:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name=name,
        level=_LEVELS.get(level, logging.INFO),
        pathname=__file__,
        lineno=0,
        msg=message,
        args=None,
        exc_info=exc_info,
    )
    record.levelname = level
    return record


class LogSink:
    """Base class of log sinks."""

    def write(self, level: str, message: str) -> None:
        """Write one message."""

    def close(self) -> None:
        """Release resources (no-op by default)."""


class FileSink(LogSink):
    """Rotating UTF-8 file sink (5 MB x 5)."""

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        self.file_path = file_path
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            file_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        self._handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        self._name = Path(file_path).stem
        self._closed = False

    def write(self, level: str, message: str) -> None:
        """Write one message through the handler (takes its lock)."""
        if self._closed:
            return
        self._handler.handle(_make_record(level, message, self._name))

    def close(self) -> None:
        """Close the file handler (idempotent)."""
        if not self._closed:
            self._closed = True
            self._handler.close()


class ConsoleSink(LogSink):
    """Stream sink (stderr by default)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        self._handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    def write(self, level: str, message: str) -> None:
        """Write one message to the stream."""
        self._handler.handle(_make_record(level, message, "console"))

    def close(self) -> None:
        """Flush the stream without closing it."""
        self._handler.flush()
