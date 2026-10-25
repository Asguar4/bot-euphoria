"""AC 18, 19 + command handlers."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from euphoria.control.commands import CommandResult, CommandSpec, build_default_commands
from euphoria.control.dispatcher import CommandDispatcher
from euphoria.control.protocol import ErrorCode
from utils.moduleManager import ModuleOpResult


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def spec(name: str, handler: Any, lo: int = 0, hi: int = 0, allowed: bool = False) -> CommandSpec:
    return CommandSpec(name, f"{name} usage", "sum", lo, hi, handler, allowed)


async def ok(_a: tuple[str, ...]) -> CommandResult:
    return CommandResult(True, "fine")


def disp(specs: list[CommandSpec], shutting: bool = False, timeout: float = 1.0) -> Any:
    return CommandDispatcher(specs, timeout=timeout, is_shutting_down=lambda: shutting)


def test_unknown() -> None:
    r = run(disp([spec("a", ok)]).dispatch("zzz", []))
    assert r.code == ErrorCode.UNKNOWN_COMMAND and not r.ok
    assert r.message == "unknown command 'zzz'; try 'help'"


@pytest.mark.parametrize(("args", "good"), [([], False), (["a"], True), (["a", "b"], False)])
def test_arity(args: list[str], good: bool) -> None:
    d = disp([spec("one", ok, 1, 1)])
    r = run(d.dispatch("one", args))
    assert (r.code == ErrorCode.OK) is good
    if not good:
        assert r.code == ErrorCode.BAD_ARGUMENTS and "usage:" in r.message


def test_timeout() -> None:
    async def slow(_a: tuple[str, ...]) -> CommandResult:
        await asyncio.sleep(5)
        return CommandResult(True, "x")

    r = run(disp([spec("s", slow)], timeout=0.05).dispatch("s", []))
    assert r.code == ErrorCode.TIMEOUT and not r.ok


def test_internal_error_no_traceback_on_wire(caplog: pytest.LogCaptureFixture) -> None:
    async def boom(_a: tuple[str, ...]) -> CommandResult:
        raise RuntimeError("secret-detail")

    with caplog.at_level(logging.ERROR):
        r = run(disp([spec("b", boom)]).dispatch("b", []))
    assert r.code == ErrorCode.INTERNAL_ERROR and r.message == "internal error, see logs"
    assert "secret-detail" not in r.message and "Traceback" not in r.message
    assert any(rec.exc_info for rec in caplog.records)


def test_cancelled_error_reraised() -> None:
    async def cancelled(_a: tuple[str, ...]) -> CommandResult:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(disp([spec("c", cancelled)]).dispatch("c", []))


def test_shutting_down_gating() -> None:
    specs = [
        spec("load", ok, 1, 1),
        spec("ping", ok, allowed=True),
        spec("status", ok, allowed=True),
        spec("exit", ok, allowed=True),
    ]
    d = disp(specs, shutting=True)
    assert run(d.dispatch("load", ["m"])).code == ErrorCode.SHUTTING_DOWN
    for n in ("ping", "status", "exit"):
        assert run(d.dispatch(n, [])).ok


def test_shutting_down_precedes_arity() -> None:
    d = disp([spec("load", ok, 1, 1)], shutting=True)
    assert run(d.dispatch("load", [])).code == ErrorCode.SHUTTING_DOWN


def test_duplicates_rejected_and_names() -> None:
    with pytest.raises(ValueError):
        disp([spec("a", ok), spec("a", ok)])
    assert disp([spec("b", ok), spec("a", ok)]).names() == ["a", "b"]


def test_dispatch_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="euphoria.control.dispatcher"):
        run(disp([spec("a", ok)]).dispatch("a", []))
    assert any("a" in r.getMessage() and "ok" in r.getMessage() for r in caplog.records)


def fake_bot() -> Any:
    bot = SimpleNamespace()
    bot.module_manager = SimpleNamespace(
        run=AsyncMock(return_value=ModuleOpResult(True, "m is loaded", True))
    )
    bot.request_shutdown = MagicMock()
    bot.extensions = {"modules.b": 1, "modules.a": 2}
    bot.latency = float("nan")
    bot.is_ready = lambda: True
    bot.user = None
    bot.guilds = [1, 2]
    bot.started_at = 0.0
    bot.shutdown = SimpleNamespace(requested=False)
    return bot


def commands_for(bot: Any) -> CommandDispatcher:
    return CommandDispatcher(
        build_default_commands(bot), timeout=1, is_shutting_down=lambda: bot.shutdown.requested
    )


@pytest.mark.parametrize("action", ["load", "reload", "unload"])
def test_module_commands_dispatch(action: str) -> None:
    bot = fake_bot()
    r = run(commands_for(bot).dispatch(action, ["modules.music"]))
    assert r.ok and r.data == {"synced": True}
    bot.module_manager.run.assert_awaited_once_with(action, "modules.music")


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",
        "a/b",
        "a\\b",
        ".a",
        "a.",
        "a..b",
        "1a",
        "",
        "a b",
        "a;b",
        "modules/../x",
        "..",
        "é",
    ],
)
@pytest.mark.parametrize("action", ["load", "reload", "unload"])
def test_bad_extension_names(action: str, bad: str) -> None:
    """AC19."""
    bot = fake_bot()
    r = run(commands_for(bot).dispatch(action, [bad]))
    assert r.code == ErrorCode.BAD_ARGUMENTS
    bot.module_manager.run.assert_not_awaited()


@pytest.mark.parametrize("good", ["a", "a.b", "_x.y1.z_", "modules.music"])
def test_good_extension_names(good: str) -> None:
    bot = fake_bot()
    assert run(commands_for(bot).dispatch("load", [good])).ok


def test_module_failure_maps_to_command_failed() -> None:
    bot = fake_bot()
    bot.module_manager.run = AsyncMock(
        return_value=ModuleOpResult(False, "failed to load x: y", False)
    )
    r = run(commands_for(bot).dispatch("load", ["x"]))
    assert not r.ok and r.code == ErrorCode.COMMAND_FAILED and r.data == {"synced": False}


def test_exit_ping_list_status_help() -> None:
    bot = fake_bot()
    d = commands_for(bot)
    r = run(d.dispatch("exit", []))
    assert r.ok and r.message == "Shutdown requested"
    bot.request_shutdown.assert_called_once_with("control: exit")
    assert run(d.dispatch("ping", [])).message == "pong"
    lst = run(d.dispatch("list", []))
    assert lst.data == ["modules.a", "modules.b"] and lst.message == "2 extension(s) loaded"
    st = run(d.dispatch("status", [])).data
    assert st["latency_ms"] is None and st["user"] is None and st["guilds"] == 2
    assert st["shutting_down"] is False and st["extensions"] == ["modules.a", "modules.b"]
    assert isinstance(st["uptime_s"], int)
    h = run(d.dispatch("help", []))
    names = [x.split(" ")[0] for x in h.data]
    assert names == sorted(names)
    assert {"load", "reload", "unload", "exit", "list", "status", "ping", "help"} <= set(names)


def test_status_latency_finite_and_user() -> None:
    bot = fake_bot()
    bot.latency = 0.0421
    bot.user = "Bot#1"
    st = run(commands_for(bot).dispatch("status", [])).data
    assert st["latency_ms"] == 42 and st["user"] == "Bot#1"


def test_allowed_during_shutdown_flags() -> None:
    by = {s.name: s for s in build_default_commands(fake_bot())}
    assert {n for n, s in by.items() if s.allowed_during_shutdown} == {
        "exit",
        "status",
        "ping",
        "help",
    }
    assert (by["load"].min_args, by["load"].max_args) == (1, 1)
    assert (by["list"].min_args, by["list"].max_args) == (0, 0)
