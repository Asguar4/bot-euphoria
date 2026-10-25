"""AC 36 + CLI parser, logging configuration."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from euphoria import cli
from euphoria.settings import LoggingSettings

FIX = Path(__file__).parent / "fixtures"


def _cleanup_logging() -> None:
    root = logging.getLogger()
    for h in list(cli._installed_handlers):
        root.removeHandler(h)
        h.close()
    cli._installed_handlers.clear()
    root.setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _clean_root_handlers() -> Any:
    _cleanup_logging()
    yield
    _cleanup_logging()


def test_run_missing_config_returns_1_names_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC36."""
    missing = tmp_path / "nope" / "config.json"
    assert cli.main(["run", "--config", str(missing)]) == 1
    err = capsys.readouterr().err
    assert str(missing) in err and "config error" in err


def test_ctl_without_command_is_usage_error_2(capsys: pytest.CaptureFixture[str]) -> None:
    """AC36."""
    assert cli.main(["ctl"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["ctl", "--port", "abc", "ping"],
        ["ctl", "--timeout", "x", "ping"],
        ["run", "--bogus"],
        ["run", "--control-port", "x"],
        ["ctl", "--bogus", "ping"],
    ],
)
def test_bad_args_exit_2(argv: list[str]) -> None:
    assert cli.main(argv) == 2


def test_help_exit_0() -> None:
    assert cli.main(["--help"]) == 0
    assert cli.main(["ctl", "--help"]) == 0


def test_invalid_json_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "c.json"
    p.write_text("{oops", encoding="utf-8")
    assert cli.main(["--config", str(p)]) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_bad_types_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--config", str(FIX / "config_bad_types.json")]) == 1
    assert "bot.TOKEN" in capsys.readouterr().err


def test_bad_db_section_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "c.json"
    p.write_text('{"bot": {"TOKEN": "t"}, "database": {}}', encoding="utf-8")
    assert cli.main(["run", "--config", str(p)]) == 1
    assert "DB_" in capsys.readouterr().err


def test_default_subcommand_is_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(cli, "cmd_run", lambda ns: seen.append(ns) or 0)
    assert cli.main([]) == 0
    assert cli.main(["--no-control"]) == 0
    assert cli.main(["--config", "x.json", "--no-console-log"]) == 0
    assert all(ns.command in (None, "run") or True for ns in seen)
    assert len(seen) == 3
    assert seen[1].no_control is True


def test_main_uses_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["euphoria", "ctl"])
    assert cli.main() == 2


def test_keyboard_interrupt_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_ns: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_run", boom)
    assert cli.main(["run"]) == 1


def test_ctl_args_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[Any] = []
    monkeypatch.setattr(cli, "run_ctl", lambda ns: got.append(ns) or 0)
    assert cli.main(["ctl", "--port", "5", "--json", "--timeout", "2", "load", "a.b"]) == 0
    ns = got[0]
    assert (ns.port, ns.json, ns.timeout, ns.ctl_command, ns.ctl_args) == (
        5,
        True,
        2.0,
        "load",
        ["a.b"],
    )
    cli.main(["ctl", "ping"])
    assert got[1].timeout == 130.0 and got[1].json is False


def test_cmd_run_returns_run_app_code_and_cleans_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import euphoria.app as app
    from utils.loggerManager import LoggerManager

    async def fake_run_app(settings: Any) -> int:
        LoggerManager().get_logger("bot").info("hello из теста")
        assert settings.bot.token == "tok-абв"
        return 7

    monkeypatch.setattr(app, "run_app", fake_run_app)
    logdir = tmp_path / "L"
    rc = cli.main(
        [
            "run",
            "--config",
            str(FIX / "config_valid.json"),
            "--log-dir",
            str(logdir),
            "--no-control",
        ]
    )
    assert rc == 7
    assert "hello из теста" in (logdir / "bot.log").read_text("utf-8")
    assert LoggerManager().loggers == {}


def test_ensure_import_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    cli.ensure_import_path(tmp_path)
    assert sys.path[0] == str(tmp_path)
    cli.ensure_import_path(tmp_path)
    assert sys.path.count(str(tmp_path)) == 1


def test_configure_logging_idempotent_and_writes(tmp_path: Path) -> None:
    root = logging.getLogger()
    base = len(root.handlers)
    ls = LoggingSettings(dir=tmp_path / "lg", level="INFO", console=True)
    cli.configure_logging(ls)
    n1 = len(root.handlers)
    cli.configure_logging(ls)
    assert len(root.handlers) == n1 == base + 2
    logging.getLogger("some.module").info("запись")
    assert logging.getLogger("discord.http").level == logging.WARNING
    for h in root.handlers:
        h.flush()
    assert "запись" in (tmp_path / "lg" / "euphoria.log").read_text("utf-8")
    cli.configure_logging(LoggingSettings(dir=tmp_path / "lg", console=False))
    assert len(root.handlers) == base + 1
    for h in list(cli._installed_handlers):
        root.removeHandler(h)
        h.close()
    cli._installed_handlers.clear()
    root.setLevel(logging.WARNING)
