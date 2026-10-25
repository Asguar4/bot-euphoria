"""Application runner: start the bot, wait for shutdown, tear down."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Final

import discord

from euphoria.bot import Bot
from euphoria.control.server import ControlServerError
from euphoria.lifecycle import ShutdownController, install_signal_handlers
from euphoria.settings import AppSettings

logger = logging.getLogger(__name__)

CANCEL_GRACE: Final = 0.5


def _default_bot_factory(settings: AppSettings, controller: ShutdownController) -> Bot:
    return Bot(settings, shutdown=controller)


def _log_start_failure(exc: BaseException) -> None:
    if isinstance(exc, discord.LoginFailure):
        logger.error("login failed: invalid token (%s)", exc)
    elif isinstance(exc, discord.PrivilegedIntentsRequired):
        logger.error("enable privileged intents in the Discord developer portal")
    elif isinstance(exc, ControlServerError):
        logger.error("control server: %s", exc)
    elif isinstance(exc, OSError):
        logger.error("startup failed (database or network): %s", exc, exc_info=exc)
    else:
        logger.error("startup failed: %r", exc, exc_info=exc)


async def run_app(
    settings: AppSettings,
    *,
    bot_factory: Callable[[AppSettings, ShutdownController], Bot] = _default_bot_factory,
) -> int:
    """Run the bot until shutdown and return the process exit code."""
    controller = ShutdownController()
    current = asyncio.current_task()
    if current is not None:
        controller.attach_task(current)
    restore = install_signal_handlers(asyncio.get_running_loop(), controller)
    exit_code = 0
    start_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[str] | None = None
    bot: Bot | None = None
    try:
        bot = bot_factory(settings, controller)
        start_task = asyncio.create_task(bot.start(settings.bot.token), name="euphoria-start")
        stop_task = asyncio.create_task(controller.wait(), name="euphoria-stop-wait")
        await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if start_task.done() and not start_task.cancelled():
            exc = start_task.exception()
            if exc is not None:
                _log_start_failure(exc)
                exit_code = 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.bot.shutdown_timeout
        teardown: asyncio.Future[Any] | None = None
        try:
            async with asyncio.timeout_at(deadline):
                await bot.close()
        except TimeoutError:
            logger.error("shutdown timed out after %ss", settings.bot.shutdown_timeout)
            teardown = getattr(bot, "_teardown_task", None)
            if teardown is not None:
                teardown.cancel()
            exit_code = 1
        if not start_task.done():
            await asyncio.wait({start_task}, timeout=max(0.0, deadline - loop.time()))
        stop_task.cancel()
        leftovers: set[asyncio.Future[Any]] = {
            t for t in (start_task, stop_task, teardown) if t is not None and not t.done()
        }
        for task in leftovers:
            task.cancel()
        if leftovers:
            _, abandoned = await asyncio.wait(leftovers, timeout=CANCEL_GRACE)
            for task in abandoned:
                logger.warning("task %r did not finish after cancel; abandoned", task)
        if start_task.done() and not start_task.cancelled():
            start_task.exception()
    except asyncio.CancelledError:
        logger.error("shutdown forced")
        exit_code = 1
    finally:
        for pending_task in (stop_task, start_task):
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()
        restore()
    return exit_code
