"""Shutdown coordination and signal handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)


class ShutdownController:
    """Single source of truth for "shutdown was requested"."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None
        self._forced = False
        self._signals = 0
        self._task: asyncio.Task[Any] | None = None

    @property
    def requested(self) -> bool:
        """Whether shutdown was requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """The first shutdown reason, if any."""
        return self._reason

    @property
    def forced(self) -> bool:
        """Whether a second signal forced the shutdown."""
        return self._forced

    def request(self, reason: str) -> None:
        """Request shutdown (idempotent, first reason wins; loop thread only)."""
        if self._reason is None:
            self._reason = reason
            logger.info("shutdown requested: %s", reason)
        self._event.set()

    async def wait(self) -> str:
        """Wait until shutdown is requested and return the reason."""
        await self._event.wait()
        return self._reason or ""

    def attach_task(self, task: asyncio.Task[Any]) -> None:
        """Register the main task that is cancelled on a forced shutdown."""
        self._task = task

    def on_signal(self, signame: str) -> None:
        """Handle a signal: first requests shutdown, second forces it."""
        self._signals += 1
        if self._signals == 1:
            self.request(f"signal {signame}")
        elif self._signals == 2:
            self._forced = True
            logger.error("forced shutdown (second %s)", signame)
            if self._task is not None:
                self._task.cancel()


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, controller: ShutdownController
) -> Callable[[], None]:
    """Install SIGINT/SIGTERM/SIGBREAK handlers feeding ``controller``.

    Uses ``signal.signal`` on all platforms (``add_signal_handler`` is unavailable
    on Windows). Must be called from the main thread.

    Returns:
        A ``restore`` callable reinstating the previous handlers (idempotent).
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning("signal handlers not installed: not in the main thread")
        return lambda: None

    names = ["SIGINT", "SIGTERM"]
    if hasattr(signal, "SIGBREAK"):
        names.append("SIGBREAK")

    def handler(signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(controller.on_signal, name)

    previous: dict[int, Any] = {}
    for name in names:
        signum = getattr(signal, name)
        try:
            previous[signum] = signal.signal(signum, handler)
        except ValueError, OSError:
            logger.warning("cannot install handler for %s", name, exc_info=True)

    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        for signum, old in previous.items():
            signal.signal(signum, old if old is not None else signal.SIG_DFL)

    return restore
