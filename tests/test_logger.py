"""Logging sinks: ConsoleSink, FileSink (lock via handle()), thread safety."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from utils.loggerManager import ConsoleSink, FileSink, Logger, LoggerManager, LogSink
from utils.loggerManager.logSink import _make_record


def test_logsink_base_is_plain_class() -> None:
    s = LogSink()
    assert s.write("INFO", "x") is None and s.close() is None
    import abc

    assert not isinstance(LogSink, abc.ABCMeta)


def test_file_sink_write_after_close_is_noop(tmp_path: Path) -> None:
    """OPT-2: a closed sink must not reopen the file."""
    path = tmp_path / "a.log"
    s = FileSink(path)
    s.write("INFO", "before")
    s.close()
    s.write("INFO", "after")
    assert s._handler.stream is None
    assert "after" not in path.read_text(encoding="utf-8")
    path.unlink()


def test_console_sink_writes_to_stream_and_close_keeps_stream() -> None:
    buf = io.StringIO()
    s = ConsoleSink(buf)
    s.write("SUCCESS", "привет")
    s.write("ERROR", "bad")
    s.close()
    assert not buf.closed
    out = buf.getvalue()
    assert "[SUCCESS] привет" in out and "[ERROR  ] bad" in out
    s.close()


def test_console_sink_default_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    s = ConsoleSink()
    s.write("INFO", "to stderr")
    s.close()
    assert "to stderr" in capsys.readouterr().err
    assert not sys.stderr.closed


def test_console_sink_exception_with_traceback() -> None:
    buf = io.StringIO()
    s = ConsoleSink(buf)
    try:
        raise KeyError("k")
    except KeyError:
        s.write("EXCEPTION", "oops")
    assert "KeyError" in buf.getvalue() and "Traceback" in buf.getvalue()


def test_file_sink_uses_handle_so_lock_is_taken(tmp_path: Path) -> None:
    s = FileSink(tmp_path / "a" / "b.log")
    calls: list[str] = []
    orig = s._handler.handle

    def spy(record: Any) -> Any:
        calls.append("handle")
        return orig(record)

    s._handler.handle = spy  # type: ignore[method-assign]
    emit_calls: list[str] = []
    orig_emit = s._handler.emit

    def emit_spy(record: Any) -> None:
        emit_calls.append("locked" if not s._handler.lock.acquire(blocking=False) else "free")
        orig_emit(record)

    s._handler.emit = emit_spy  # type: ignore[method-assign]
    s.write("INFO", "msg")
    s.close()
    assert calls == ["handle"]
    assert emit_calls
    assert s.file_path == tmp_path / "a" / "b.log"


def test_file_sink_delay_no_file_until_write(tmp_path: Path) -> None:
    p = tmp_path / "d" / "x.log"
    s = FileSink(p)
    assert p.parent.exists() and not p.exists()
    s.write("INFO", "x")
    assert p.exists()
    s.close()
    s.close()
    p.unlink()


def test_file_sink_threads_no_lost_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.log"
    s = FileSink(p)
    n_threads, per = 8, 200

    def work(i: int) -> None:
        for j in range(per):
            s.write("INFO", f"t{i}-{j}-ъ")

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    s.close()
    lines = p.read_text("utf-8").splitlines()
    assert len(lines) == n_threads * per
    assert all(line.startswith("[") and line.endswith("-ъ") for line in lines)


def test_file_sink_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "r.log"
    s = FileSink(p)
    assert s._handler.maxBytes == 5 * 1024 * 1024 and s._handler.backupCount == 5
    s._handler.maxBytes = 2000
    for i in range(200):
        s.write("INFO", "x" * 50 + str(i))
    s.close()
    assert (tmp_path / "r.log.1").exists()
    p.unlink()
    for f in tmp_path.glob("r.log.*"):
        f.unlink()


def test_make_record_levels() -> None:
    assert _make_record("SUCCESS", "m", "n").levelname == "SUCCESS"
    assert _make_record("WEIRD", "m", "n").levelno == 20
    assert _make_record("EXCEPTION", "m", "n").exc_info is None


def test_logger_never_raises_even_with_bad_sink() -> None:
    class Bad(LogSink):
        def write(self, level: str, message: str) -> None:
            raise RuntimeError("x")

    log = Logger()
    log.sinks.append(Bad())
    for name in ("debug", "info", "success", "warning", "error", "exception"):
        getattr(log, name)("m")


def test_remove_logger_closes_sinks(tmp_path: Path) -> None:
    lm = LoggerManager()
    lm.configure(tmp_path / "rm")
    lg = lm.create_logger("zz")
    lg.info("x")
    lm.remove_logger("zz")
    (tmp_path / "rm" / "zz.log").unlink()


def test_create_logger_thread_safe(tmp_path: Path) -> None:
    lm = LoggerManager()
    lm.configure(tmp_path / "ts")
    got: list[Logger] = []

    def mk() -> None:
        got.append(lm.create_logger("same"))

    ts = [threading.Thread(target=mk) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(x) for x in got}) == 1
    assert len(got[0].sinks) == 1


def test_shutdown_ignores_failing_close() -> None:
    class Bad(LogSink):
        def close(self) -> None:
            raise RuntimeError("x")

    lm = LoggerManager()
    lg = lm.create_logger("bad")
    lg.sinks.append(Bad())
    lm.shutdown()
    assert lm.loggers == {}
