"""Round 3: OPT-3 budget sum, OPT-4 first-line timeout, OPT-8 validation, OPT-12 warning."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import pytest
from test_config import RAW_MIN
from test_control_server import make_server, req

from euphoria.bot import Bot
from euphoria.settings import ConfigError, load_settings


def test_default_teardown_budget_fits_shutdown_timeout(make_settings: Any) -> None:
    """OPT-3: worst-case sum of step budgets <= shutdown_timeout with defaults."""
    bot = Bot(make_settings(enabled=False))
    st = bot.settings
    st = dataclasses.replace(st, bot=dataclasses.replace(st.bot, shutdown_timeout=30.0))
    bot.settings = st
    t = Bot.STEP_TIMEOUTS
    total = (
        t["_stop_control"]
        + bot._unload_budget()
        + t["_close_gateway"]
        + min(t["_close_database"], st.database.close_timeout + 1.5)
    )
    assert total <= 30.0, (total, bot._unload_budget(), st.database.close_timeout)


@pytest.mark.parametrize("sto", [0.5, 5.0, 30.0, 120.0])
def test_unload_budget_bounds(make_settings: Any, sto: float) -> None:
    bot = Bot(make_settings(enabled=False))
    bot.settings = dataclasses.replace(
        bot.settings, bot=dataclasses.replace(bot.settings.bot, shutdown_timeout=sto)
    )
    b = bot._unload_budget()
    assert 1.0 <= b <= 30.0


def test_opt4_slow_authenticated_client_not_cut(tmp_path: Path) -> None:
    """OPT-4: after the first line, a client may idle longer than FIRST_LINE_TIMEOUT."""
    import euphoria.control.server as S

    async def go() -> tuple[bytes, bytes]:
        old = S.FIRST_LINE_TIMEOUT
        S.FIRST_LINE_TIMEOUT = 0.3
        try:
            srv = make_server(tmp_path, idle_timeout=5.0)
            await srv.start()
            r, w = await asyncio.open_connection(*srv.address)  # type: ignore[misc]
            w.write(req(srv))
            a = await asyncio.wait_for(r.readline(), 2)
            await asyncio.sleep(0.8)
            w.write(req(srv))
            b = await asyncio.wait_for(r.readline(), 2)
            w.close()
            await srv.stop()
            return a, b
        finally:
            S.FIRST_LINE_TIMEOUT = old

    a, b = asyncio.run(go())
    assert b'"ok": true' in a.replace(b'":true', b'": true') and b"pong" in b


def test_opt4_unauthenticated_idle_closed_and_slow_first_line_within_limit(
    tmp_path: Path,
) -> None:
    import euphoria.control.server as S

    async def go() -> tuple[bytes, bytes]:
        old = S.FIRST_LINE_TIMEOUT
        S.FIRST_LINE_TIMEOUT = 0.4
        try:
            srv = make_server(tmp_path, idle_timeout=30.0)
            await srv.start()
            r1, w1 = await asyncio.open_connection(*srv.address)  # type: ignore[misc]
            idle = await asyncio.wait_for(r1.read(), 2)
            r2, w2 = await asyncio.open_connection(*srv.address)  # type: ignore[misc]
            await asyncio.sleep(0.15)
            w2.write(req(srv))
            ok = await asyncio.wait_for(r2.readline(), 2)
            w1.close()
            w2.close()
            await srv.stop()
            return idle, ok
        finally:
            S.FIRST_LINE_TIMEOUT = old

    idle, ok = asyncio.run(go())
    assert idle == b"" and b"pong" in ok


def test_opt4_idle_timeout_smaller_than_first_line(tmp_path: Path) -> None:
    async def go() -> bytes:
        srv = make_server(tmp_path, idle_timeout=0.3)
        await srv.start()
        r, w = await asyncio.open_connection(*srv.address)  # type: ignore[misc]
        data = await asyncio.wait_for(r.read(), 2)
        w.close()
        await srv.stop()
        return data

    assert asyncio.run(go()) == b""


@pytest.mark.parametrize("key", ["MAX_CONNECTIONS", "IDLE_TIMEOUT", "COMMAND_TIMEOUT"])
@pytest.mark.parametrize("val", [0, -1, -0.5, 0.0])
def test_opt8_nonpositive_rejected(key: str, val: float, tmp_path: Path) -> None:
    raw = {**RAW_MIN, "control": {key: val}}
    with pytest.raises(ConfigError, match=key):
        load_settings(raw, base_dir=tmp_path)


@pytest.mark.parametrize(
    ("key", "val"),
    [
        ("MAX_CONNECTIONS", 1),
        ("IDLE_TIMEOUT", 0.001),
        ("IDLE_TIMEOUT", 1),
        ("COMMAND_TIMEOUT", 0.001),
    ],
)
def test_opt8_small_positive_accepted(key: str, val: float, tmp_path: Path) -> None:
    s = load_settings({**RAW_MIN, "control": {key: val}}, base_dir=tmp_path)
    assert getattr(s.control, key.lower()) == val


@pytest.mark.parametrize("key", ["MAX_CONNECTIONS", "IDLE_TIMEOUT"])
@pytest.mark.parametrize("val", [True, "5", [1]])
def test_opt8_bad_types_rejected(key: str, val: Any, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings({**RAW_MIN, "control": {key: val}}, base_dir=tmp_path)


def test_opt8_max_connections_float_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings({**RAW_MIN, "control": {"MAX_CONNECTIONS": 2.5}}, base_dir=tmp_path)


def test_opt12_sync_job_warns_and_signature_frozen(make_settings: Any) -> None:
    bot = Bot(make_settings(enabled=False))
    sig = inspect.signature(Bot.add_scheduler_task)
    assert list(sig.parameters) == ["self", "func", "day", "hour", "minute", "id", "args"]
    assert sig.parameters["args"].default == ()

    def sync_job() -> None: ...

    async def async_job() -> None: ...

    calls: list[str] = []
    orig = bot.logger.warning
    bot.logger.warning = lambda m, *a, **k: (calls.append(m), orig(m))[1]  # type: ignore[method-assign]
    bot.add_scheduler_task(sync_job, "*", 3, 0, "s1")
    bot.add_scheduler_task(async_job, "*", 3, 0, "a1")
    bot.add_scheduler_task(sync_job, "*", 3, 0, "s2", ())
    assert any("s1" in c for c in calls) and any("s2" in c for c in calls)
    assert not any("a1" in c for c in calls)
    ids = {j.id for j in bot.scheduler.get_jobs()}
    assert {"s1", "a1", "s2"} <= ids
