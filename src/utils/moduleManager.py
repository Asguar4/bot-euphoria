"""Load, reload and unload bot extensions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from utils.loggerManager import LoggerManager

type ModuleAction = Literal["load", "reload", "unload"]

_PAST = {"load": "loaded", "reload": "reloaded", "unload": "unloaded"}


@dataclass(frozen=True, slots=True)
class ModuleOpResult:
    """Outcome of an extension operation."""

    ok: bool
    message: str
    synced: bool


class ModuleManager:
    """Serialised extension management for a bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._log = LoggerManager().get_logger("bot")

    async def run(self, action: ModuleAction, name: str) -> ModuleOpResult:
        """Perform an extension action and sync the command tree on success.

        Args:
            action: One of ``load``, ``reload``, ``unload``.
            name: Extension module name.

        Returns:
            The operation result; extension errors are reported, not raised.
        """
        async with self._lock:
            operation = getattr(self.bot, f"{action}_extension")
            try:
                await operation(name)
            except (commands.ExtensionError, ImportError) as e:
                self._log.exception(f"ModuleManager: failed to {action} {name}: {e}")
                return ModuleOpResult(False, f"failed to {action} {name}: {e}", False)
            message = f"{name} is {_PAST[action]}"
            self._log.success(f"ModuleManager: {message}")
            try:
                await self.bot.tree.sync()
            except (
                discord.HTTPException,
                discord.ClientException,
                app_commands.AppCommandError,
            ) as e:
                self._log.error(f"ModuleManager: command tree sync failed: {e}")
                return ModuleOpResult(True, f"{message}; command tree sync failed: {e}", False)
            return ModuleOpResult(True, message, True)

    async def load(self, name: str) -> str:
        """Load an extension and return a status message."""
        return (await self.run("load", name)).message

    async def unload(self, name: str) -> str:
        """Unload an extension and return a status message."""
        return (await self.run("unload", name)).message

    async def reload(self, name: str) -> str:
        """Reload an extension and return a status message."""
        return (await self.run("reload", name)).message

    async def exit(self) -> None:
        """Request bot shutdown and return immediately."""
        self._log.info("ModuleManager: executing bot shutdown command")
        bot: Any = self.bot
        request = getattr(bot, "request_shutdown", None)
        if request is not None:
            request("module manager exit")
        else:
            await bot.close()
