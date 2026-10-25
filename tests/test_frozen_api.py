"""AC 6: frozen API signatures (inspect.signature)."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from utils.config import Config
from utils.database import Database
from utils.loggerManager import ConsoleSink, FileSink, Logger, LoggerManager, LogSink
from utils.managerPermission import ManagerPermission
from utils.moduleManager import ModuleManager

ROOT = Path(__file__).resolve().parents[1]


def params(fn: Any) -> list[tuple[str, object, object]]:
    sig = inspect.signature(fn)
    return [(p.name, p.kind, p.default) for p in sig.parameters.values()]


E = inspect.Parameter.empty
POS = inspect.Parameter.POSITIONAL_OR_KEYWORD
KW = inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (Database.__init__, [("self", POS, E), ("settings", KW, None)]),
        (Database.get_pool, [("self", POS, E)]),
        (Database.close, [("self", POS, E)]),
        (Database.execute, [("self", POS, E), ("query", POS, E), ("params", POS, ())]),
        (Database.fetchall, [("self", POS, E), ("query", POS, E), ("params", POS, ())]),
        (
            Database.add,
            [("self", POS, E), ("id", POS, E), ("info", POS, None), ("base", POS, "activities")],
        ),
        (Database.get, [("self", POS, E), ("id", POS, E), ("base", POS, "activities")]),
        (
            Database.set,
            [("self", POS, E), ("id", POS, E), ("values", POS, E), ("base", POS, "activities")],
        ),
        (Database.delete, [("self", POS, E), ("id", POS, E), ("base", POS, "activities")]),
        (ModuleManager.__init__, [("self", POS, E), ("bot", POS, E)]),
        (ModuleManager.load, [("self", POS, E), ("name", POS, E)]),
        (ModuleManager.unload, [("self", POS, E), ("name", POS, E)]),
        (ModuleManager.reload, [("self", POS, E), ("name", POS, E)]),
        (ModuleManager.exit, [("self", POS, E)]),
        (ModuleManager.run, [("self", POS, E), ("action", POS, E), ("name", POS, E)]),
        (Config.__init__, [("self", POS, E)]),
        (Config.update_config, [("self", POS, E)]),
        (LogSink.write, [("self", POS, E), ("level", POS, E), ("message", POS, E)]),
        (LogSink.close, [("self", POS, E)]),
        (FileSink.__init__, [("self", POS, E), ("file_path", POS, E)]),
        (ConsoleSink.__init__, [("self", POS, E), ("stream", POS, None)]),
        (Logger.__init__, [("self", POS, E)]),
        (LoggerManager.__init__, [("self", POS, E)]),
        (LoggerManager.create_logger, [("self", POS, E), ("name", POS, E)]),
        (LoggerManager.get_logger, [("self", POS, E), ("name", POS, E)]),
        (LoggerManager.remove_logger, [("self", POS, E), ("name", POS, E)]),
        (
            LoggerManager.configure,
            [("self", POS, E), ("log_dir", POS, None), ("console", KW, None)],
        ),
        (LoggerManager.shutdown, [("self", POS, E)]),
    ],
)
def test_signature(fn: Any, expected: list[tuple[str, object, object]]) -> None:
    assert params(fn) == expected


@pytest.mark.parametrize("name", ["debug", "info", "success", "warning", "error", "exception"])
def test_logger_methods(name: str) -> None:
    assert params(getattr(Logger, name)) == [("self", POS, E), ("message", POS, E)]


@pytest.mark.parametrize("name", ["isOwner", "isOwnerApp"])
def test_permission_noarg(name: str) -> None:
    assert params(getattr(ManagerPermission, name)) == []
    assert isinstance(inspect.getattr_static(ManagerPermission, name), staticmethod)


@pytest.mark.parametrize("name", ["isRole", "isUser"])
def test_permission_id(name: str) -> None:
    assert params(getattr(ManagerPermission, name)) == [("id", POS, E)]
    assert isinstance(inspect.getattr_static(ManagerPermission, name), staticmethod)


def test_bot_signatures() -> None:
    from euphoria.bot import Bot

    assert params(Bot.__init__) == [
        ("self", POS, E),
        ("settings", POS, None),
        ("database", KW, None),
        ("shutdown", KW, None),
    ]
    assert params(Bot.add_scheduler_task) == [
        ("self", POS, E),
        ("func", POS, E),
        ("day", POS, E),
        ("hour", POS, E),
        ("minute", POS, E),
        ("id", POS, E),
        ("args", POS, ()),
    ]
    assert params(Bot.on_app_command_error) == [
        ("self", POS, E),
        ("interaction", POS, E),
        ("error", POS, E),
    ]
    assert params(Bot.request_shutdown) == [("self", POS, E), ("reason", POS, E)]
    assert inspect.iscoroutinefunction(Bot.close)
    assert params(Bot.close) == [("self", POS, E)]


def test_async_flags() -> None:
    for name in ("get_pool", "close", "execute", "fetchall", "add", "get", "set", "delete"):
        assert inspect.iscoroutinefunction(getattr(Database, name)), name
    for name in ("load", "unload", "reload", "exit", "run"):
        assert inspect.iscoroutinefunction(getattr(ModuleManager, name)), name


def test_main_shim_reexports_bot() -> None:
    import main
    from euphoria.bot import Bot

    assert main.Bot is Bot


def test_loggermanager_star_import_and_all() -> None:
    ns: dict[str, object] = {}
    exec("from utils.loggerManager import *", ns)
    import utils.loggerManager as lm

    assert set(lm.__all__) == {"LogSink", "ConsoleSink", "FileSink", "Logger", "LoggerManager"}
    for n in lm.__all__:
        assert n in ns


def test_config_new_symbols() -> None:
    import utils.config as c

    assert params(c.set_config_path) == [("path", POS, E)]
    assert params(c.get_config_path) == []
    assert issubclass(c.ConfigError, Exception)


def test_database_new_symbols() -> None:
    import utils.database as d

    assert hasattr(d, "DatabaseSettings")
    assert isinstance(Database().converter, dict)


def test_loggermanager_log_dir_property() -> None:
    assert isinstance(inspect.getattr_static(LoggerManager, "log_dir"), property)


def test_bot_instance_attributes(make_settings: Any) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from euphoria.bot import Bot

    bot = Bot(make_settings(enabled=False))
    assert isinstance(bot.logger, Logger)
    assert isinstance(bot.database, Database)
    assert isinstance(bot.scheduler, AsyncIOScheduler)
    assert isinstance(bot.module_manager, ModuleManager)
    assert str(bot.scheduler.timezone) == "Europe/Moscow"
    assert bot.logger is LoggerManager().get_logger("bot")
    assert bot.module_manager._log is bot.logger
    assert not bot.scheduler.running


def test_inputmanager_removed() -> None:
    assert not (ROOT / "src" / "utils" / "inputManager").exists()
    assert not (ROOT / "src" / "__init__.py").exists()
    r = subprocess.run(
        [sys.executable, "-c", "import utils.inputManager"],
        cwd=ROOT / "src",
        capture_output=True,
    )
    assert r.returncode != 0
