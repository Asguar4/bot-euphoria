"""AC 15, 16: ModuleManager."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from utils.moduleManager import ModuleManager, ModuleOpResult


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_bot(**kw: Any) -> Any:
    bot = MagicMock()
    bot.load_extension = AsyncMock()
    bot.reload_extension = AsyncMock()
    bot.unload_extension = AsyncMock()
    bot.tree.sync = AsyncMock()
    for k, v in kw.items():
        setattr(bot, k, v)
    return bot


@pytest.mark.parametrize(
    ("action", "past"), [("load", "loaded"), ("reload", "reloaded"), ("unload", "unloaded")]
)
def test_success_messages(action: str, past: str) -> None:
    bot = make_bot()
    mm = ModuleManager(bot)
    msg = run(getattr(mm, action)("modules.x"))
    assert msg == f"modules.x is {past}"
    assert isinstance(msg, str)
    getattr(bot, f"{action}_extension").assert_awaited_once_with("modules.x")
    bot.tree.sync.assert_awaited_once()


@pytest.mark.parametrize("action", ["load", "reload", "unload"])
def test_failure_returns_str_no_sync(action: str) -> None:
    bot = make_bot()
    getattr(bot, f"{action}_extension").side_effect = commands.ExtensionNotFound("nope")
    msg = run(getattr(ModuleManager(bot), action)("nope"))
    assert isinstance(msg, str)
    assert msg.startswith(f"failed to {action} nope:")
    bot.tree.sync.assert_not_awaited()


def test_failure_result_object() -> None:
    bot = make_bot()
    bot.load_extension.side_effect = commands.ExtensionFailed("n", ValueError("bad"))
    res = run(ModuleManager(bot).run("load", "n"))
    assert res == ModuleOpResult(False, res.message, False)
    assert res.message.startswith("failed to load n:")


def test_sync_http_exception_not_raised() -> None:
    bot = make_bot()
    bot.tree.sync.side_effect = discord.HTTPException(MagicMock(status=429, reason="r"), "limited")
    res = run(ModuleManager(bot).run("load", "m"))
    assert res.ok is True and res.synced is False
    assert "m is loaded" in res.message and "sync failed" in res.message
    assert isinstance(run(ModuleManager(bot).load("m")), str)


@pytest.mark.parametrize(
    "exc", [discord.ClientException("not logged in"), commands.CommandError("x")]
)
def test_sync_other_expected_errors(exc: Exception) -> None:
    from discord import app_commands

    bot = make_bot()
    bot.tree.sync.side_effect = (
        app_commands.AppCommandError("x") if isinstance(exc, commands.CommandError) else exc
    )
    res = run(ModuleManager(bot).run("load", "m"))
    assert res.ok and not res.synced


def test_unexpected_exception_propagates() -> None:
    bot = make_bot()
    bot.load_extension.side_effect = RuntimeError("weird")
    with pytest.raises(RuntimeError):
        run(ModuleManager(bot).run("load", "m"))


def test_concurrent_runs_serialised() -> None:
    events: list[tuple[str, str]] = []

    async def slow(name: str) -> None:
        events.append(("start", name))
        await asyncio.sleep(0.05)
        events.append(("end", name))

    bot = make_bot(load_extension=slow, reload_extension=slow)

    async def go() -> None:
        mm = ModuleManager(bot)
        await asyncio.gather(mm.run("load", "a"), mm.run("reload", "b"), mm.run("load", "c"))

    run(go())
    for i in range(0, 6, 2):
        assert events[i][0] == "start" and events[i + 1] == ("end", events[i][1])


def test_exit_requests_shutdown_without_close() -> None:
    bot = make_bot()
    bot.close = AsyncMock()
    run(ModuleManager(bot).exit())
    bot.request_shutdown.assert_called_once()
    bot.close.assert_not_awaited()


def test_exit_fallback_close_without_request_shutdown() -> None:
    class Plain:
        close = AsyncMock()

    run(ModuleManager(Plain()).exit())  # type: ignore[arg-type]
    Plain.close.assert_awaited_once()


def test_logger_is_bot_logger() -> None:
    from utils.loggerManager import LoggerManager

    assert ModuleManager(make_bot())._log is LoggerManager().get_logger("bot")


@pytest.mark.parametrize("action", ["load", "reload", "unload"])
def test_missing_parent_package_returns_str_not_raises(action: str) -> None:
    """AC15 / user decision: load/reload/unload always return str (real discord bot).

    ``bot.load_extension("nopkg.mod")`` raises a bare ModuleNotFoundError (from
    importlib.util.find_spec), which is not an ExtensionError. Expected: a ``failed to ...`` str.
    """
    from euphoria.bot import Bot
    from utils.moduleManager import ModuleManager as MM

    async def go() -> str:
        import dataclasses

        from euphoria.settings import (
            AppSettings,
            BotSettings,
            ControlSettings,
            LoggingSettings,
        )
        from utils.database import DatabaseSettings

        s = AppSettings(
            BotSettings("t"),
            ControlSettings(enabled=False),
            LoggingSettings(),
            DatabaseSettings("h", 1, "u", "p", "d"),
        )
        del dataclasses
        bot = Bot(s)
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]
        return await getattr(MM(bot), action)("nopkg_xyz.mod")

    msg = run(go())
    assert isinstance(msg, str) and msg.startswith(f"failed to {action} nopkg_xyz.mod:")
