"""AC 37, 38 + setup_hook / on_ready behaviour."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands

from euphoria.bot import Bot
from utils.moduleManager import ModuleOpResult


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_interaction(done: bool) -> Any:
    i = MagicMock()
    i.response.is_done.return_value = done
    i.response.send_message = AsyncMock()
    i.followup.send = AsyncMock()
    i.command = "cmd"
    return i


PERM = "You don't have permission to use this command"


def test_check_failure_message_not_done(make_settings: Any) -> None:
    """AC37."""
    bot = Bot(make_settings(enabled=False))
    i = make_interaction(False)
    run(bot.on_app_command_error(i, app_commands.CheckFailure("x")))
    i.response.send_message.assert_awaited_once_with(PERM, ephemeral=True)
    i.followup.send.assert_not_awaited()


def test_check_failure_message_done(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    i = make_interaction(True)
    run(bot.on_app_command_error(i, app_commands.MissingPermissions(["x"])))
    i.followup.send.assert_awaited_once_with(PERM, ephemeral=True)
    i.response.send_message.assert_not_awaited()


@pytest.mark.parametrize("done", [True, False])
def test_other_error_message(make_settings: Any, done: bool) -> None:
    bot = Bot(make_settings(enabled=False))
    i = make_interaction(done)
    err = app_commands.AppCommandError("kaboom ъ")
    run(bot.on_app_command_error(i, err))
    expected = f"An error occurred while executing the command:\n{err}"
    target = i.followup.send if done else i.response.send_message
    target.assert_awaited_once_with(expected, ephemeral=True)


def test_command_invoke_error_unwrapped_and_logged(make_settings: Any, tmp_path: Any) -> None:
    from utils.loggerManager import LoggerManager

    bot = Bot(make_settings(enabled=False))
    i = make_interaction(False)

    async def cb(_i: Any) -> None:
        return None

    cmd = MagicMock(spec=app_commands.Command)
    cmd.name = "x"

    async def go() -> None:
        try:
            raise ValueError("inner-problem")
        except ValueError as e:
            await bot.on_app_command_error(i, app_commands.CommandInvokeError(cmd, e))

    run(go())
    LoggerManager().shutdown()
    text = (tmp_path / "logs" / "bot.log").read_text("utf-8")
    assert "inner-problem" in text and "Traceback" in text
    i.response.send_message.assert_awaited_once()


@pytest.mark.parametrize("exc_cls", [discord.NotFound, discord.HTTPException, discord.Forbidden])
@pytest.mark.parametrize("done", [True, False])
def test_send_failure_swallowed(make_settings: Any, exc_cls: type, done: bool) -> None:
    """AC37."""
    bot = Bot(make_settings(enabled=False))
    i = make_interaction(done)
    exc = exc_cls(MagicMock(status=404, reason="r"), "gone")
    (i.followup.send if done else i.response.send_message).side_effect = exc
    run(bot.on_app_command_error(i, app_commands.AppCommandError("e")))


def test_tree_on_error_hooked(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    assert bot.tree.on_error == bot.on_app_command_error


def test_add_scheduler_task_before_setup_hook(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    calls: list[int] = []

    def job(*a: int) -> None:
        calls.extend(a)

    bot.add_scheduler_task(job, "mon-fri", 3, "*/5", "job1", (1, 2))
    j = bot.scheduler.get_job("job1")
    assert j is not None
    assert j.args == (1, 2)
    assert j.misfire_grace_time == 3600 and j.coalesce is True
    expected = CronTrigger(day_of_week="mon-fri", hour=3, minute="*/5")
    assert str(j.trigger) == str(expected)
    assert j.trigger.timezone.key == "Europe/Moscow" or str(j.trigger.timezone) == "Europe/Moscow"


def test_replace_existing_after_start(make_settings: Any) -> None:
    async def go() -> None:
        bot = Bot(make_settings(enabled=False))
        bot.add_scheduler_task(lambda: None, "*", 3, 0, "dup")
        bot.add_scheduler_task(lambda: None, "*", 4, 0, "dup")
        bot.scheduler.start()
        assert len(bot.scheduler.get_jobs()) == 1
        bot.add_scheduler_task(lambda: None, "*", 5, 0, "dup")
        assert len(bot.scheduler.get_jobs()) == 1
        await bot.close()

    run(go())


def test_add_scheduler_task_defaults_args(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    bot.add_scheduler_task(lambda: None, "*", 1, 1, "j")
    job = bot.scheduler.get_job("j")
    assert job is not None and tuple(job.args) == ()


@pytest.mark.parametrize(
    ("day", "hour", "minute"), [("*", 99, 0), ("xyz", 1, 1), ("*", 1, 61), ("*", "a", "b")]
)
def test_invalid_trigger_logged_not_raised(
    make_settings: Any, tmp_path: Any, day: Any, hour: Any, minute: Any
) -> None:
    from utils.loggerManager import LoggerManager

    bot = Bot(make_settings(enabled=False))
    bot.add_scheduler_task(lambda: None, day, hour, minute, "bad")
    assert bot.scheduler.get_job("bad") is None
    LoggerManager().shutdown()
    text = (tmp_path / "logs" / "bot.log").read_text("utf-8")
    assert "failed to add scheduler task bad" in text and "Traceback" in text


def test_add_scheduler_task_works_after_start(make_settings: Any) -> None:
    async def go() -> None:
        bot = Bot(make_settings(enabled=False))
        bot.scheduler.start()
        bot.add_scheduler_task(lambda: None, "*", 1, 1, "late")
        assert bot.scheduler.get_job("late") is not None
        await bot.close()

    run(go())


def test_setup_hook_eager_connect_and_control(make_settings: Any) -> None:
    async def go() -> None:
        db = MagicMock()
        db.get_pool = AsyncMock()
        db.close = AsyncMock()
        s = make_settings()
        bot = Bot(s, database=db)
        await bot.setup_hook()
        db.get_pool.assert_awaited_once()
        assert bot.scheduler.running
        assert bot.control_server is not None and bot.control_server.is_serving
        state = json.loads(s.control.state_file.read_text("utf-8"))
        assert state["port"] == bot.control_server.address[1]  # type: ignore[index]
        await bot.close()

    run(go())


def test_setup_hook_lazy_when_eager_false(make_settings: Any, db_settings: Any) -> None:
    import dataclasses

    from euphoria.settings import AppSettings

    async def go() -> None:
        s: AppSettings = make_settings(enabled=False)
        s = dataclasses.replace(s, database=dataclasses.replace(db_settings, eager_connect=False))
        db = MagicMock()
        db.get_pool = AsyncMock()
        db.close = AsyncMock()
        bot = Bot(s, database=db)
        await bot.setup_hook()
        db.get_pool.assert_not_awaited()
        await bot.close()

    run(go())


def test_setup_hook_db_failure_aborts_startup(make_settings: Any) -> None:
    async def go() -> None:
        db = MagicMock()
        db.get_pool = AsyncMock(side_effect=OSError("refused"))
        db.close = AsyncMock()
        bot = Bot(make_settings(enabled=False), database=db)
        with pytest.raises(OSError):
            await bot.setup_hook()
        await bot.close()
        db.close.assert_awaited_once()
        assert not bot.scheduler.running

    run(go())


def test_setup_hook_autoloads_extensions_and_survives_failure(
    make_settings: Any, db_settings: Any
) -> None:
    import dataclasses

    async def go() -> list[tuple[str, str]]:
        s = make_settings(enabled=False)
        s = dataclasses.replace(
            s,
            bot=dataclasses.replace(s.bot, extensions=("ext.bad", "ext.good")),
            database=dataclasses.replace(db_settings, eager_connect=False),
        )
        bot = Bot(s)
        calls: list[tuple[str, str]] = []

        async def fake_run(action: str, name: str) -> ModuleOpResult:
            calls.append((action, name))
            return ModuleOpResult(name != "ext.bad", "msg", True)

        bot.module_manager.run = fake_run  # type: ignore[method-assign]
        await bot.setup_hook()
        await bot.close()
        return calls

    assert run(go()) == [("load", "ext.bad"), ("load", "ext.good")]


def test_setup_hook_control_port_in_use(make_settings: Any) -> None:
    from euphoria.control.server import ControlServerError

    async def go() -> None:
        a = Bot(make_settings(), database=MagicMock(get_pool=AsyncMock(), close=AsyncMock()))
        await a.setup_hook()
        port = a.control_server.address[1]  # type: ignore[union-attr]
        b = Bot(
            make_settings(port=port, state_file=a.settings.control.state_file.parent / "o.json"),
            database=MagicMock(get_pool=AsyncMock(), close=AsyncMock()),
        )
        with pytest.raises(ControlServerError):
            await b.setup_hook()
        await b.close()
        await a.close()

    run(go())


def test_on_ready_syncs_once_and_survives_http_error(make_settings: Any) -> None:
    async def go() -> int:
        bot = Bot(make_settings(enabled=False))
        bot.change_presence = AsyncMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock(  # type: ignore[method-assign]
            side_effect=discord.HTTPException(MagicMock(status=500, reason="r"), "x")
        )
        await bot.on_ready()
        await bot.on_ready()
        return int(bot.tree.sync.await_count)

    assert run(go()) == 1


def test_request_shutdown_nonblocking(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    bot.request_shutdown("cog says so")
    assert bot.shutdown.requested and bot.shutdown.reason == "cog says so"


def test_uses_given_database_and_shutdown(make_settings: Any) -> None:
    from euphoria.lifecycle import ShutdownController

    db, sc = MagicMock(), ShutdownController()
    bot = Bot(make_settings(enabled=False), database=db, shutdown=sc)
    assert bot.database is db and bot.shutdown is sc


def test_zero_arg_bot_reads_cfg_lazily(tmp_path: Any, fake_pool: Any) -> None:
    """Bot() uses cfg; reading happens at construction, not at import."""
    from utils.config import set_config_path

    cfg = {
        "bot": {"TOKEN": "t"},
        "database": {
            "DB_HOST": "h",
            "DB_PORT": 1,
            "DB_USER": "u",
            "DB_PASSWORD": "p",
            "DB_NAME": "d",
        },
        "control": {"ENABLED": False},
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    set_config_path(p)
    bot = Bot()
    assert bot.settings.bot.token == "t" and bot.control_server is None


def test_logging_levels_in_namespace() -> None:
    assert logging.getLogger("euphoria.bot").name == "euphoria.bot"
    assert SimpleNamespace


def test_setup_hook_autoload_missing_parent_package_does_not_abort(
    make_settings: Any, db_settings: Any
) -> None:
    """DESIGN 8.14 setup_hook step 4: a failed startup extension must not abort startup."""
    import dataclasses

    async def go() -> None:
        s = make_settings(enabled=False)
        s = dataclasses.replace(
            s,
            bot=dataclasses.replace(s.bot, extensions=("nopkg_abc.mod",)),
            database=dataclasses.replace(db_settings, eager_connect=False),
        )
        bot = Bot(s)
        try:
            await bot.setup_hook()
        finally:
            await bot.close()

    run(go())
