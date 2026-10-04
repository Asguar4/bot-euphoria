import os
import sys
import logging
from logging.handlers import RotatingFileHandler

class LogSink:
    def write(self, level: str, message: str):
        pass

# соответствие строковых уровней числовым уровням logging
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "EXCEPTION": logging.ERROR,
}


class FileSink(LogSink):
    def __init__(self, file_path: str):
        self.file_path = file_path
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)

        self._handler = RotatingFileHandler(
            file_path,
            maxBytes=5 * 1024 * 1024,  # 5 МБ
            backupCount=5,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)-7s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    def write(self, level: str, message: str):
        exc_info = None
        if level == "EXCEPTION" and sys.exc_info()[0] is not None:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="Logger",
            level=_LEVELS.get(level, logging.INFO),
            pathname=__file__,
            lineno=0,
            msg=message,
            args=None,
            exc_info=exc_info,
        )
        record.levelname = level
        self._handler.emit(record)

    def close(self):
        self._handler.close()
