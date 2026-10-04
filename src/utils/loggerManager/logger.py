from .logSink import LogSink


class Logger:
    def __init__(self):
        self.sinks: list[LogSink] = []

    def _dispatch(self, level: str, message: str):
        # некорректное сообщение — приводим к строке
        if not isinstance(message, str):
            message = str(message)

        # ошибка одного обработчика не должна останавливать работу остальных
        for sink in self.sinks:
            try:
                sink.write(level, message)
            except Exception:
                continue

    def debug(self, message: str):
        self._dispatch("DEBUG", message)

    def info(self, message: str):
        self._dispatch("INFO", message)

    def success(self, message: str):
        self._dispatch("SUCCESS", message)

    def warning(self, message: str):
        self._dispatch("WARNING", message)

    def error(self, message: str):
        self._dispatch("ERROR", message)

    def exception(self, message: str):
        self._dispatch("EXCEPTION", message)
