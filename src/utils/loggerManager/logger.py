"""Facade logger dispatching to sinks."""

import logging

from .logSink import LogSink

_internal = logging.getLogger(__name__)


class Logger:
    """Logger with level methods (including ``success``) writing to sinks."""

    def __init__(self) -> None:
        self.sinks: list[LogSink] = []
        self._reported: set[int] = set()

    def _dispatch(self, level: str, message: str) -> None:
        if not isinstance(message, str):
            message = str(message)
        for sink in self.sinks:
            try:
                sink.write(level, message)
            except Exception:
                if id(sink) not in self._reported:
                    self._reported.add(id(sink))
                    _internal.warning("log sink %r failed", sink, exc_info=True)

    def debug(self, message: str) -> None:
        """Log at DEBUG."""
        self._dispatch("DEBUG", message)

    def info(self, message: str) -> None:
        """Log at INFO."""
        self._dispatch("INFO", message)

    def success(self, message: str) -> None:
        """Log a success message (INFO severity)."""
        self._dispatch("SUCCESS", message)

    def warning(self, message: str) -> None:
        """Log at WARNING."""
        self._dispatch("WARNING", message)

    def error(self, message: str) -> None:
        """Log at ERROR."""
        self._dispatch("ERROR", message)

    def exception(self, message: str) -> None:
        """Log at ERROR including the active exception traceback."""
        self._dispatch("EXCEPTION", message)
