import os
from .logger import Logger
from .logSink import FileSink

# путь к логам относительно этого файла, а не откуда запустили скрипт
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = os.path.join(_BASE_DIR, "logs")

os.makedirs(_LOG_DIR, exist_ok=True)


class LoggerManager:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not LoggerManager._initialized:
            self.loggers = {}
            LoggerManager._initialized = True

    def create_logger(self, name: str) -> Logger:
        if name not in self.loggers:
            logger = Logger()
            # logger.sinks.append(ConsoleSink())
            logger.sinks.append(FileSink(os.path.join(_LOG_DIR, f"{name}.log")))
            self.loggers[name] = logger
        return self.loggers[name]

    def get_logger(self, name: str) -> Logger:
        if name not in self.loggers:
            return self.create_logger(name)
        return self.loggers[name]

    def remove_logger(self, name: str):
        if name in self.loggers:
            logger = self.loggers.pop(name)
            for sink in logger.sinks:
                if hasattr(sink, "close"):
                    sink.close()
