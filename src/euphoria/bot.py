"""The Discord bot with an ordered, idempotent teardown."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from euphoria.control.commands import build_default_commands
from euphoria.control.dispatcher import CommandDispatcher
from euphoria.control.server import ControlServer
from euphoria.lifecycle import ShutdownController
from euphoria.settings import AppSettings, load_settings
from utils import config as config_module
from utils.database import Database
from utils.loggerManager import LoggerManager
from utils.moduleManager import ModuleManager

logger = logging.getLogger(__name__)

PERMISSION_DENIED = "You don't have permission to use this command"


class Bot(commands.Bot):
    """Euphoria bot; ``close()`` performs the ordered teardown exactly once."""

    STEP_TIMEOUTS: ClassVar[dict[str, float]] = {
        "_stop_control": 3.0,
        "_unload_extensions": 30.0,
        "_close_gateway": 10.0,
        "_close_database": 10.0,
    }
    EXTENSION_UNLOAD_TIMEOUT = 5.0
    MIN_UNLOAD_BUDGET = 1.0
    TEARDOWN_MARGIN = 1.5

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        database: Database | None = None,
        shutdown: ShutdownController | None = None,
    ) -> None:
        super().__init__(command_prefix="/", help_command=None, intents=discord.Intents.all())
        self.settings = settings or load_settings(
            config_module.cfg, base_dir=config_module.get_config_path().parent
        )
        self.logger = LoggerManager().get_logger("bot")
        self.database = database or Database(settings=self.settings.database)
        self.scheduler = AsyncIOScheduler(timezone=self.settings.bot.timezone)
        self.shutdown = shutdown or ShutdownController()
        self.module_manager = ModuleManager(self)
        self.control_server: ControlServer | None = None
        if self.settings.control.enabled:
            dispatcher = CommandDispatcher(
                build_default_commands(self),
                timeout=self.settings.control.command_timeout,
                is_shutting_down=lambda: self.shutdown.requested,
            )
            self.control_server = ControlServer(self.settings.control, dispatcher)
        self.started_at = time.monotonic()
        self._synced = False
        self._teardown_task: asyncio.Task[None] | None = None
        self.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]

    async def setup_hook(self) -> None:
        """Start the scheduler, DB pool, control server and configured extensions."""
        self.scheduler.start()
        self.logger.success("Scheduler started")
        if self.settings.database.eager_connect:
            await self.database.get_pool()
            self.logger.success("Database connected successfully")
        if self.control_server is not None:
            await self.control_server.start()
        for ext in self.settings.bot.extensions:
            result = await self.module_manager.run("load", ext)
            if not result.ok:
                logger.error("startup extension failed: %s", result.message)

    def add_scheduler_task(
        self,
        func: Callable[..., Any],
        day: int | str,
        hour: int | str,
        minute: int | str,
        id: str,
        args: tuple[Any, ...] = (),
    ) -> None:
        """Add a cron job; never raises (errors are logged)."""
        if not inspect.iscoroutinefunction(func):
            self.logger.warning(
                f"scheduler job {id} is not async def; it will run in the default executor "
                "and may delay process exit"
            )
        try:
            self.scheduler.add_job(
                func,
                CronTrigger(day_of_week=day, hour=hour, minute=minute),
                id=id,
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
                args=args,
            )
            self.logger.success(f"{id} added to scheduler")
        except Exception as e:
            self.logger.exception(f"failed to add scheduler task {id}: {e}")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Log the error and tell the user (ephemeral)."""
        original: BaseException = error
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original
        if isinstance(error, app_commands.CheckFailure):
            self.logger.info(f"check failed for /{interaction.command}: {error}")
            message = PERMISSION_DENIED
        else:
            self.logger.exception(f"app command error: {original!r}")
            message = f"An error occurred while executing the command:\n{error}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException as e:
            self.logger.warning(f"could not send error message to user: {e}")

    async def on_ready(self) -> None:
        """Set presence and sync the tree once."""
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name="/help"),
            status=discord.Status.online,
        )
        if not self._synced:
            self._synced = True
            try:
                await self.tree.sync()
            except discord.HTTPException as e:
                self.logger.error(f"initial command tree sync failed: {e}")
            self.logger.success(f"Bot {self.user} started successfully")

    def request_shutdown(self, reason: str) -> None:
        """Ask for shutdown without blocking."""
        self.shutdown.request(reason)

    async def close(self) -> None:
        """Run the teardown once; concurrent and repeated calls share it."""
        self.shutdown.request("close() called")
        if self._teardown_task is None:
            self._teardown_task = asyncio.create_task(self._teardown(), name="euphoria-teardown")
        await asyncio.shield(self._teardown_task)

    def _unload_budget(self) -> float:
        """Time for the unload step that still leaves room for the later steps."""
        reserve = (
            self.STEP_TIMEOUTS["_close_gateway"]
            + self.settings.database.close_timeout
            + self.TEARDOWN_MARGIN
        )
        available = self.settings.bot.shutdown_timeout - self.STEP_TIMEOUTS["_stop_control"]
        return min(
            self.STEP_TIMEOUTS["_unload_extensions"],
            max(self.MIN_UNLOAD_BUDGET, available - reserve),
        )

    async def _run_step(
        self, name: str, step: Callable[[], Awaitable[None]], timeout: float | None = None
    ) -> None:
        if timeout is None:
            timeout = self.STEP_TIMEOUTS.get(name)
        try:
            async with asyncio.timeout(timeout):
                await step()
        except TimeoutError:
            logger.error("teardown step %s timed out after %ss", name, timeout)
        except Exception:
            logger.exception("teardown step %s failed", name)

    async def _teardown(self) -> None:
        await self._run_step("_stop_control", self._stop_control)
        try:
            self._stop_scheduler()
        except Exception:
            logger.exception("teardown step _stop_scheduler failed")
        await self._run_step(
            "_unload_extensions", self._unload_extensions, timeout=self._unload_budget()
        )
        await self._run_step("_close_gateway", self._close_gateway)
        await self._run_step("_close_database", self._close_database)
        self.logger.info(f"Bot stopped (reason: {self.shutdown.reason})")

    async def _stop_control(self) -> None:
        if self.control_server is not None:
            await self.control_server.stop(grace=2.0)

    def _stop_scheduler(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def _unload_extensions(self) -> None:
        for name in reversed(list(self.extensions)):
            try:
                async with asyncio.timeout(self.EXTENSION_UNLOAD_TIMEOUT):
                    await self.unload_extension(name)
            except Exception:
                logger.exception("failed to unload extension %s", name)
        for cog_name in list(self.cogs):
            try:
                await self.remove_cog(cog_name)
            except Exception:
                logger.exception("failed to remove cog %s", cog_name)

    async def _close_gateway(self) -> None:
        await super().close()

    async def _close_database(self) -> None:
        await self.database.close()
