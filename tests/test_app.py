"""AC 34, 35: run_app."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from pathlib import Path
from typing import Any

import discord
import pytest

from euphoria.app import CANCEL_GRACE, run_app
from euphoria.bot import Bot
from euphoria.control.client import Endpoint, send_command
from euphoria.control.server import ControlServerError
from euphoria.lifecycle import ShutdownController
from euphoria.settings import AppSettings

STEPS = [
    "_stop_control",
    "_stop_scheduler",
    "_unload_extensions",
    "_close_gateway",
    "_close_database",
]


class Holder:
    bot: Bot | None = None
    closes = 0
    steps: list[str]


def factory_for(
    start: Any,
    steps: list[str],
    hang_step: str | None = None,
    close_counter: list[int] | None = None,
) -> Any:
    def factory(settings: AppSettings, controller: ShutdownController) -> Bot:
        bot = Bot(settings, shutdown=controller)
        orig_close = bot.close

        async def counted_close() -> None:
            if close_counter is not None:
                close_counter.append(1)
            await orig_close()

        bot.close = counted_close  # type: ignore[method-assign]

        for name in STEPS:
            real = getattr(bot, name)

            def wrap(n: str = name, r: Any = real) -> Any:
                if n == "_stop_scheduler":

                    def sync() -> None:
                        steps.append(n)
                        r()

                    return sync

                async def a() -> None:
                    steps.append(n)
                    if n == hang_step:
                        await asyncio.sleep(3600)
                    await r()

                return a

            setattr(bot, name, wrap())

        async def bound_start(token: str, *, reconnect: bool = True) -> None:
            await start(bot, token)

        bot.start = bound_start  # type: ignore[method-assign]
        return bot

    return factory


async def wait_forever(_bot: Bot, _token: str) -> None:
    await asyncio.sleep(3600)


async def until_closed(bot: Bot, _token: str) -> None:
    await bot.setup_hook()
    while not bot.is_closed():
        await asyncio.sleep(0.02)


def test_request_shutdown_returns_0_and_close_awaited(make_settings: Any) -> None:
    """AC34 a."""
    steps: list[str] = []
    closes: list[int] = []
    s = _short_timeout(make_settings(enabled=False), 0.5)

    async def go() -> int:
        holder: dict[str, ShutdownController] = {}
        inner = factory_for(wait_forever, steps, close_counter=closes)

        def fac(settings: AppSettings, controller: ShutdownController) -> Bot:
            holder["c"] = controller
            return inner(settings, controller)

        task = asyncio.create_task(run_app(s, bot_factory=fac))
        for _ in range(100):
            if "c" in holder:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        holder["c"].request("test")
        return await asyncio.wait_for(task, 15)

    t = time.perf_counter()
    assert asyncio.run(go()) == 0
    assert closes and steps == STEPS
    assert time.perf_counter() - t < 0.5 + CANCEL_GRACE + 0.5


def test_start_returning_normally_exit_0(make_settings: Any) -> None:
    steps: list[str] = []

    async def returns(_b: Bot, _t: str) -> None:
        return None

    assert (
        asyncio.run(run_app(make_settings(enabled=False), bot_factory=factory_for(returns, steps)))
        == 0
    )
    assert steps == STEPS


@pytest.mark.parametrize(
    "exc",
    [
        discord.LoginFailure("Improper token"),
        discord.PrivilegedIntentsRequired(None),
        ControlServerError("cannot bind"),
        OSError("db down"),
        RuntimeError("weird"),
    ],
)
def test_start_failure_returns_1_with_teardown(make_settings: Any, exc: Exception) -> None:
    """AC34 b."""
    steps: list[str] = []

    async def fails(_b: Bot, _t: str) -> None:
        raise exc

    rc = asyncio.run(run_app(make_settings(enabled=False), bot_factory=factory_for(fails, steps)))
    assert rc == 1
    assert steps == STEPS


def test_bot_factory_failure_does_not_hang(make_settings: Any) -> None:
    def bad(_s: AppSettings, _c: ShutdownController) -> Bot:
        raise RuntimeError("factory broke")

    with pytest.raises(RuntimeError):
        asyncio.run(run_app(make_settings(enabled=False), bot_factory=bad))


def _short_timeout(s: AppSettings, t: float) -> AppSettings:
    return dataclasses.replace(s, bot=dataclasses.replace(s.bot, shutdown_timeout=t))


def test_teardown_timeout_returns_1_quickly_when_start_ends(make_settings: Any) -> None:
    """AC34 c (start() returns once shutdown is requested)."""
    s = _short_timeout(make_settings(enabled=False), 0.3)
    steps: list[str] = []

    async def self_stop(bot: Bot, _t: str) -> None:
        await asyncio.sleep(0.05)
        bot.request_shutdown("test")
        while not bot.is_closed() and not bot.shutdown.requested:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)

    t = time.perf_counter()
    rc = asyncio.run(
        run_app(s, bot_factory=factory_for(self_stop, steps, hang_step="_unload_extensions"))
    )
    elapsed = time.perf_counter() - t
    assert rc == 1
    assert elapsed < 2, elapsed


def test_teardown_timeout_with_never_ending_start_documented(make_settings: Any) -> None:
    """AC34 c, worst case: start() never returns => extra 5 s grace (DESIGN 8.15 step 5)."""
    s = _short_timeout(make_settings(enabled=False), 0.3)
    steps: list[str] = []

    async def self_stop(bot: Bot, _t: str) -> None:
        await asyncio.sleep(0.05)
        bot.request_shutdown("test")
        await asyncio.sleep(3600)

    t = time.perf_counter()
    rc = asyncio.run(
        run_app(s, bot_factory=factory_for(self_stop, steps, hang_step="_unload_extensions"))
    )
    elapsed = time.perf_counter() - t
    assert rc == 1
    assert elapsed < 1.0


def test_forced_second_signal_returns_1(make_settings: Any) -> None:
    s = make_settings(enabled=False)
    steps: list[str] = []

    async def go() -> int:
        holder: dict[str, ShutdownController] = {}
        inner = factory_for(wait_forever, steps, hang_step="_unload_extensions")

        def fac(settings: AppSettings, controller: ShutdownController) -> Bot:
            holder["c"] = controller
            return inner(settings, controller)

        task = asyncio.create_task(run_app(s, bot_factory=fac))
        await asyncio.sleep(0.2)
        holder["c"].on_signal("SIGINT")
        await asyncio.sleep(0.2)
        assert not task.done()
        holder["c"].on_signal("SIGINT")
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(go()) == 1


def test_signal_handlers_restored_after_run_app(make_settings: Any) -> None:
    import signal

    async def returns(_b: Bot, _t: str) -> None:
        return None

    async def go() -> None:
        before = signal.getsignal(signal.SIGINT)
        await run_app(make_settings(enabled=False), bot_factory=factory_for(returns, []))
        assert signal.getsignal(signal.SIGINT) == before

    asyncio.run(go())


def test_end_to_end_ctl_exit(make_settings: Any, fake_pool: Any, tmp_path: Path) -> None:
    """AC35: real control server on port 0, exit through the real client."""
    s: AppSettings = make_settings()
    results: dict[str, Any] = {}

    async def go() -> int:
        task = asyncio.create_task(run_app(s, bot_factory=factory_for(until_closed, [])))
        sf = s.control.state_file
        for _ in range(200):
            if sf.exists():
                break
            await asyncio.sleep(0.02)
        import json

        st = json.loads(sf.read_text("utf-8"))
        ep = Endpoint(st["host"], st["port"], st["token"])
        results["status"] = await asyncio.to_thread(send_command, ep, "status", [], timeout=5)
        results["exit"] = await asyncio.to_thread(send_command, ep, "exit", [], timeout=5)
        rc = await asyncio.wait_for(task, 15)
        left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        results["left"] = left
        return rc

    rc = asyncio.run(go())
    assert rc == 0
    assert results["status"].ok and results["status"].data["shutting_down"] is False
    assert results["exit"].ok and results["exit"].message == "Shutdown requested"
    assert results["left"] == []
    assert not s.control.state_file.exists()
    assert fake_pool.created == 1 and fake_pool.closed
    extra = [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
    assert extra == []


def test_load_via_control_during_run_then_exit(make_settings: Any, fake_pool: Any) -> None:
    """Control commands behave while running: load of a missing ext reports failure."""
    s: AppSettings = make_settings()
    out: dict[str, Any] = {}

    async def go() -> int:
        task = asyncio.create_task(run_app(s, bot_factory=factory_for(until_closed, [])))
        sf = s.control.state_file
        for _ in range(200):
            if sf.exists():
                break
            await asyncio.sleep(0.02)
        import json

        st = json.loads(sf.read_text("utf-8"))
        ep = Endpoint(st["host"], st["port"], st["token"])
        out["load"] = await asyncio.to_thread(
            send_command, ep, "load", ["euphoria.nope"], timeout=5
        )
        out["bad"] = await asyncio.to_thread(send_command, ep, "load", ["../x"], timeout=5)
        out["list"] = await asyncio.to_thread(send_command, ep, "list", [], timeout=5)
        out["help"] = await asyncio.to_thread(send_command, ep, "help", [], timeout=5)
        await asyncio.to_thread(send_command, ep, "exit", [], timeout=5)
        return await asyncio.wait_for(task, 15)

    assert asyncio.run(go()) == 0
    assert not out["load"].ok and out["load"].message.startswith("failed to load euphoria.nope:")
    assert out["bad"].code == "bad_arguments"
    assert out["list"].data == []
    assert len(out["help"].data) == 8


def test_ac34d_fast_teardown_never_returning_start_exit_0(make_settings: Any) -> None:
    """AC34 d: fast teardown, start() ignores close() => rc 0 within timeout + 0.7 s."""
    timeout = 0.4
    s = _short_timeout(make_settings(enabled=False), timeout)
    steps: list[str] = []
    state: dict[str, Any] = {"cancelled": False}

    async def never(bot: Bot, _t: str) -> None:
        await asyncio.sleep(0.05)
        bot.request_shutdown("test")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    t = time.perf_counter()
    rc = asyncio.run(run_app(s, bot_factory=factory_for(never, steps)))
    elapsed = time.perf_counter() - t
    assert rc == 0
    assert steps == STEPS
    assert state["cancelled"], "start_task was not cancelled"
    assert elapsed < timeout + 0.7, elapsed


def test_slow_unload_does_not_starve_gateway_and_database(
    make_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPT-3: unload budget is derived so later steps still run within shutdown_timeout."""
    s = _short_timeout(make_settings(enabled=False), 3.0)
    monkeypatch.setattr(
        Bot,
        "STEP_TIMEOUTS",
        {
            "_stop_control": 0.3,
            "_unload_extensions": 3.0,
            "_close_gateway": 0.5,
            "_close_database": 1.0,
        },
    )
    monkeypatch.setattr(Bot, "TEARDOWN_MARGIN", 0.5)
    calls: list[str] = []

    async def never(self: Bot, _token: str) -> None:
        self.shutdown.request("test")
        await asyncio.Event().wait()

    async def slow_unload(_self: Bot) -> None:
        calls.append("unload")
        await asyncio.sleep(10)

    async def gateway(_self: Bot) -> None:
        calls.append("gateway")

    async def database(_self: Bot) -> None:
        calls.append("database")

    monkeypatch.setattr(Bot, "start", never)
    monkeypatch.setattr(Bot, "_unload_extensions", slow_unload)
    monkeypatch.setattr(Bot, "_close_gateway", gateway)
    monkeypatch.setattr(Bot, "_close_database", database)
    t = time.perf_counter()
    asyncio.run(run_app(s))
    assert calls == ["unload", "gateway", "database"]
    assert time.perf_counter() - t < 3.0 + CANCEL_GRACE
