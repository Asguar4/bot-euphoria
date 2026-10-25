"""AC 30-33: ordered, idempotent, isolated teardown of Bot."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from euphoria.bot import Bot

STEPS = [
    "_stop_control",
    "_stop_scheduler",
    "_unload_extensions",
    "_close_gateway",
    "_close_database",
]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def record_steps(
    bot: Bot, order: list[str], overrides: dict[str, Callable[..., Any]] | None = None
) -> None:
    overrides = overrides or {}
    for name in STEPS:
        if name in overrides:
            setattr(bot, name, overrides[name])
            continue
        if name == "_stop_scheduler":
            setattr(bot, name, lambda n=name: order.append(n))
        else:

            async def step(n: str = name) -> None:
                order.append(n)

            setattr(bot, name, step)


def make_bot(make_settings: Any, **kw: Any) -> Bot:
    return Bot(make_settings(enabled=False), **kw)


def test_order_exact(make_settings: Any) -> None:
    """AC30."""

    async def go() -> list[str]:
        bot = make_bot(make_settings)
        order: list[str] = []
        record_steps(bot, order)
        await bot.close()
        return order

    assert run(go()) == STEPS


def test_concurrent_close_runs_teardown_once(make_settings: Any) -> None:
    """AC30."""

    async def go() -> list[str]:
        bot = make_bot(make_settings)
        order: list[str] = []

        async def slow() -> None:
            order.append("_unload_extensions")
            await asyncio.sleep(0.1)

        record_steps(bot, order, {"_unload_extensions": slow})
        await asyncio.gather(bot.close(), bot.close(), bot.close())
        await bot.close()
        return order

    assert run(go()) == STEPS


def test_close_requests_shutdown_reason(make_settings: Any) -> None:
    async def go() -> tuple[bool, str | None]:
        bot = make_bot(make_settings)
        record_steps(bot, [])
        await bot.close()
        return bot.shutdown.requested, bot.shutdown.reason

    assert run(go()) == (True, "close() called")


def test_first_reason_preserved(make_settings: Any) -> None:
    async def go() -> str | None:
        bot = make_bot(make_settings)
        record_steps(bot, [])
        bot.request_shutdown("because")
        await bot.close()
        return bot.shutdown.reason

    assert run(go()) == "because"


@pytest.mark.parametrize("failing", STEPS)
def test_step_raising_does_not_block_later_steps(make_settings: Any, failing: str) -> None:
    """AC31 (raise)."""

    async def go() -> list[str]:
        bot = make_bot(make_settings)
        order: list[str] = []

        def boom_sync() -> None:
            order.append(failing)
            raise RuntimeError("boom")

        async def boom() -> None:
            order.append(failing)
            raise RuntimeError("boom")

        record_steps(bot, order, {failing: boom_sync if failing == "_stop_scheduler" else boom})
        await bot.close()
        return order

    assert run(go()) == STEPS


@pytest.mark.parametrize(
    "hanging", ["_stop_control", "_unload_extensions", "_close_gateway", "_close_database"]
)
def test_step_hanging_does_not_block_later_steps(
    make_settings: Any, monkeypatch: pytest.MonkeyPatch, hanging: str
) -> None:
    """AC31 (hang past its timeout)."""
    monkeypatch.setattr(Bot, "STEP_TIMEOUTS", {k: 0.1 for k in Bot.STEP_TIMEOUTS})

    async def go() -> tuple[list[str], float]:
        bot = make_bot(make_settings)
        order: list[str] = []

        async def hang() -> None:
            order.append(hanging)
            await asyncio.sleep(3600)

        record_steps(bot, order, {hanging: hang})
        t = time.perf_counter()
        await bot.close()
        return order, time.perf_counter() - t

    order, elapsed = run(go())
    assert order == STEPS and elapsed < 2


def test_extension_unload_reverse_order_no_sync(
    make_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC32."""
    pkg = tmp_path / "mods"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for n in "abc":
        (pkg / f"ext_{n}.py").write_text(
            "import teardown_log\n"
            "async def setup(bot):\n    pass\n"
            f"async def teardown(bot):\n    teardown_log.events.append('{n}')\n",
            encoding="utf-8",
        )
    (tmp_path / "teardown_log.py").write_text("events = []\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for mod in [m for m in sys.modules if m.startswith(("mods", "teardown_log"))]:
        del sys.modules[mod]

    async def go() -> list[str]:
        import teardown_log

        bot = make_bot(make_settings)
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]
        for n in ("c", "a", "b"):
            await bot.load_extension(f"mods.ext_{n}")
        assert list(bot.extensions) == ["mods.ext_c", "mods.ext_a", "mods.ext_b"]
        await bot._unload_extensions()
        assert not bot.extensions
        bot.tree.sync.assert_not_awaited()
        return list(teardown_log.events)

    assert run(go()) == ["b", "a", "c"]


def test_unload_failure_isolated_and_cogs_removed(make_settings: Any) -> None:
    async def go() -> None:
        bot = make_bot(make_settings)

        class C(commands.Cog):
            pass

        await bot.add_cog(C())
        assert "C" in bot.cogs
        await bot._unload_extensions()
        assert "C" not in bot.cogs

    run(go())


def test_unload_extension_hang_is_bounded(
    make_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Bot, "EXTENSION_UNLOAD_TIMEOUT", 0.1)

    async def go() -> list[str]:
        bot = make_bot(make_settings)
        seen: list[str] = []

        async def hang(name: str) -> None:
            seen.append(name)
            await asyncio.sleep(3600)

        monkeypatch.setattr(Bot, "extensions", property(lambda s: {"x": 1, "y": 2}))
        bot.unload_extension = hang  # type: ignore[method-assign]
        await bot._unload_extensions()
        return seen

    assert run(go()) == ["y", "x"]


def test_close_database_after_gateway_with_real_methods(
    make_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC32: _close_database runs after _close_gateway (real step methods)."""
    order: list[str] = []

    async def fake_client_close(self: Any) -> None:
        order.append("gateway")

    monkeypatch.setattr(commands.Bot, "close", fake_client_close)

    async def go() -> None:
        db = MagicMock()

        async def db_close() -> None:
            order.append("db")

        db.close = db_close
        bot = Bot(make_settings(enabled=False), database=db)
        await bot.close()

    run(go())
    assert order == ["gateway", "db"]


def test_scheduler_not_running_after_teardown(make_settings: Any) -> None:
    """AC32."""

    async def go() -> bool:
        bot = make_bot(make_settings)
        bot.scheduler.start()
        assert bot.scheduler.running
        await bot.close()
        return bool(bot.scheduler.running)

    assert run(go()) is False


def test_scheduler_job_does_not_fire_after_stop(make_settings: Any) -> None:
    async def go() -> int:
        bot = make_bot(make_settings)
        bot.scheduler.start()
        hits: list[int] = []
        bot.scheduler.add_job(lambda: hits.append(1), "interval", seconds=0.05)
        await asyncio.sleep(0.2)
        n_before = len(hits)
        await bot.close()
        await asyncio.sleep(0.3)
        assert len(hits) <= n_before + 1
        return len(hits)

    assert run(go()) >= 1


def test_close_without_setup_hook(make_settings: Any) -> None:
    """AC33: real steps, nothing started."""

    async def go() -> None:
        bot = Bot(make_settings())
        await bot.close()
        assert not bot.scheduler.running
        assert bot.control_server is not None and not bot.control_server.is_serving
        await bot.close()

    run(go())


def test_close_without_setup_hook_control_disabled(make_settings: Any) -> None:
    async def go() -> None:
        await make_bot(make_settings).close()

    run(go())


def test_control_stop_removes_state_file_during_teardown(make_settings: Any) -> None:
    async def go() -> None:
        settings = make_settings()
        bot = Bot(settings)
        assert bot.control_server is not None
        await bot.control_server.start()
        sf = settings.control.state_file
        assert sf.exists() and json.loads(sf.read_text("utf-8"))["port"] > 0
        await bot.close()
        assert not sf.exists()
        assert not bot.control_server.is_serving

    run(go())


def test_database_real_close_called_and_pool_not_recreated(
    make_settings: Any, fake_pool: Any
) -> None:
    async def go() -> None:
        bot = Bot(make_settings(enabled=False))
        await bot.database.get_pool()
        await bot.close()
        assert fake_pool.closed
        with pytest.raises(RuntimeError):
            await bot.database.get_pool()

    run(go())
    assert fake_pool.created == 1


def test_no_leftover_threads_or_tasks_after_full_teardown(
    make_settings: Any, fake_pool: Any
) -> None:
    before = set(threading.enumerate())

    async def go() -> None:
        bot = Bot(make_settings())
        bot.scheduler.start()
        await bot.database.get_pool()
        assert bot.control_server is not None
        await bot.control_server.start()
        await bot.close()
        await asyncio.sleep(0.05)
        left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert left == [], left

    run(go())
    extra = [t for t in threading.enumerate() if t not in before and not t.daemon]
    assert extra == []


def test_final_log_line_written(make_settings: Any, tmp_path: Path) -> None:
    async def go() -> None:
        bot = make_bot(make_settings)
        record_steps(bot, [])
        bot.request_shutdown("unit test")
        await bot.close()

    run(go())
    from utils.loggerManager import LoggerManager

    LoggerManager().shutdown()
    text = (tmp_path / "logs" / "bot.log").read_text("utf-8")
    assert "Bot stopped (reason: unit test)" in text


def test_teardown_task_cancel_is_safe(make_settings: Any) -> None:
    """Cancelling the caller of close() must not cancel the shielded teardown."""

    async def go() -> list[str]:
        bot = make_bot(make_settings)
        order: list[str] = []

        async def slow() -> None:
            order.append("_unload_extensions")
            await asyncio.sleep(0.2)

        record_steps(bot, order, {"_unload_extensions": slow})
        t = asyncio.create_task(bot.close())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert bot._teardown_task is not None
        await bot._teardown_task
        return order

    assert run(go()) == STEPS
