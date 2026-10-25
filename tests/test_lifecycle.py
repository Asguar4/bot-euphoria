"""AC 29: ShutdownController and signal handlers."""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import Any

import pytest

from euphoria.lifecycle import ShutdownController, install_signal_handlers


def test_request_first_reason_wins_and_wait() -> None:
    async def go() -> None:
        c = ShutdownController()
        assert not c.requested and c.reason is None and not c.forced
        c.request("one")
        c.request("two")
        assert c.requested and c.reason == "one"
        assert await asyncio.wait_for(c.wait(), 1) == "one"

    asyncio.run(go())


def test_wait_blocks_until_request() -> None:
    async def go() -> None:
        c = ShutdownController()
        w = asyncio.create_task(c.wait())
        await asyncio.sleep(0.05)
        assert not w.done()
        c.request("x")
        assert await w == "x"

    asyncio.run(go())


def test_on_signal_first_requests_second_forces_and_cancels() -> None:
    async def go() -> None:
        c = ShutdownController()

        async def main() -> None:
            await asyncio.sleep(30)

        t = asyncio.create_task(main())
        c.attach_task(t)
        c.on_signal("SIGINT")
        assert c.reason == "signal SIGINT" and not c.forced
        await asyncio.sleep(0)
        assert not t.cancelled() and not t.done()
        c.on_signal("SIGTERM")
        assert c.forced
        with pytest.raises(asyncio.CancelledError):
            await t
        c.on_signal("SIGTERM")
        assert c.reason == "signal SIGINT"

    asyncio.run(go())


def test_second_signal_without_task() -> None:
    async def go() -> None:
        c = ShutdownController()
        c.on_signal("SIGINT")
        c.on_signal("SIGINT")
        assert c.forced

    asyncio.run(go())


def _names() -> list[str]:
    n = ["SIGINT", "SIGTERM"]
    if hasattr(signal, "SIGBREAK"):
        n.append("SIGBREAK")
    return n


def test_install_and_restore_signal_handlers() -> None:
    outer = {n: signal.getsignal(getattr(signal, n)) for n in _names()}

    async def go() -> None:
        before = {n: signal.getsignal(getattr(signal, n)) for n in _names()}
        loop = asyncio.get_running_loop()
        c = ShutdownController()
        restore = install_signal_handlers(loop, c)
        for n in _names():
            assert signal.getsignal(getattr(signal, n)) is not before[n]
        restore()
        for n in _names():
            assert signal.getsignal(getattr(signal, n)) == before[n]
        restore()
        for n in _names():
            assert signal.getsignal(getattr(signal, n)) == before[n]

    asyncio.run(go())
    for n in _names():
        assert signal.getsignal(getattr(signal, n)) == outer[n]


def test_signal_delivery_requests_shutdown() -> None:
    async def go() -> None:
        loop = asyncio.get_running_loop()
        c = ShutdownController()
        restore = install_signal_handlers(loop, c)
        try:
            signal.raise_signal(signal.SIGINT)
            assert await asyncio.wait_for(c.wait(), 2) == "signal SIGINT"
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.1)
            assert c.forced
        finally:
            restore()

    asyncio.run(go())


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="Windows only")
def test_sigbreak_handled() -> None:
    async def go() -> None:
        c = ShutdownController()
        restore = install_signal_handlers(asyncio.get_running_loop(), c)
        try:
            signal.raise_signal(signal.SIGBREAK)
            assert await asyncio.wait_for(c.wait(), 2) == "signal SIGBREAK"
        finally:
            restore()

    asyncio.run(go())


def test_install_from_non_main_thread_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    before = signal.getsignal(signal.SIGINT)
    result: list[Any] = []

    def worker() -> None:
        async def go() -> None:
            restore = install_signal_handlers(asyncio.get_running_loop(), ShutdownController())
            result.append(restore)
            restore()

        asyncio.run(go())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result and signal.getsignal(signal.SIGINT) == before
    assert any("main thread" in r.getMessage() for r in caplog.records)


def test_signal_after_loop_closed_does_not_raise() -> None:
    loop = asyncio.new_event_loop()
    c = ShutdownController()
    restore = install_signal_handlers(loop, c)
    try:
        loop.close()
        signal.raise_signal(signal.SIGINT)
    finally:
        restore()
