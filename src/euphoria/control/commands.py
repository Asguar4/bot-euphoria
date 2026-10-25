"""Control command registry."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from euphoria.control.protocol import ErrorCode
from utils.moduleManager import ModuleAction

if TYPE_CHECKING:
    from euphoria.bot import Bot

_EXT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of a command handler."""

    ok: bool
    message: str
    code: ErrorCode = ErrorCode.OK
    data: dict[str, Any] | list[Any] | None = None


type CommandHandler = Callable[[tuple[str, ...]], Awaitable[CommandResult]]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Description of one control command."""

    name: str
    usage: str
    summary: str
    min_args: int
    max_args: int
    handler: CommandHandler
    allowed_during_shutdown: bool = False


def build_default_commands(bot: Bot) -> list[CommandSpec]:
    """Build the default command set bound to ``bot``."""

    def module_command(action: ModuleAction) -> CommandHandler:
        async def handler(args: tuple[str, ...]) -> CommandResult:
            ext = args[0]
            if not _EXT_RE.fullmatch(ext):
                return CommandResult(
                    False, f"invalid extension name {ext!r}", ErrorCode.BAD_ARGUMENTS
                )
            res = await bot.module_manager.run(action, ext)
            return CommandResult(
                res.ok,
                res.message,
                ErrorCode.OK if res.ok else ErrorCode.COMMAND_FAILED,
                {"synced": res.synced},
            )

        return handler

    async def exit_(_args: tuple[str, ...]) -> CommandResult:
        bot.request_shutdown("control: exit")
        return CommandResult(True, "Shutdown requested")

    async def list_(_args: tuple[str, ...]) -> CommandResult:
        names = sorted(bot.extensions)
        return CommandResult(True, f"{len(names)} extension(s) loaded", data=names)

    async def status(_args: tuple[str, ...]) -> CommandResult:
        latency = bot.latency
        data = {
            "ready": bot.is_ready(),
            "user": str(bot.user) if bot.user else None,
            "latency_ms": round(latency * 1000) if math.isfinite(latency) else None,
            "guilds": len(bot.guilds),
            "extensions": sorted(bot.extensions),
            "uptime_s": int(time.monotonic() - bot.started_at),
            "shutting_down": bot.shutdown.requested,
        }
        return CommandResult(True, "ok", data=data)

    async def ping(_args: tuple[str, ...]) -> CommandResult:
        return CommandResult(True, "pong")

    specs: list[CommandSpec] = []

    async def help_(_args: tuple[str, ...]) -> CommandResult:
        lines = [f"{s.usage} - {s.summary}" for s in sorted(specs, key=lambda s: s.name)]
        return CommandResult(True, f"{len(lines)} command(s)", data=lines)

    specs.extend(
        [
            CommandSpec(
                "load", "load <extension>", "load an extension", 1, 1, module_command("load")
            ),
            CommandSpec(
                "reload",
                "reload <extension>",
                "reload an extension",
                1,
                1,
                module_command("reload"),
            ),
            CommandSpec(
                "unload",
                "unload <extension>",
                "unload an extension",
                1,
                1,
                module_command("unload"),
            ),
            CommandSpec("exit", "exit", "shut the bot down gracefully", 0, 0, exit_, True),
            CommandSpec("list", "list", "list loaded extensions", 0, 0, list_),
            CommandSpec("status", "status", "show bot status", 0, 0, status, True),
            CommandSpec("ping", "ping", "check the control channel", 0, 0, ping, True),
            CommandSpec("help", "help", "list commands", 0, 0, help_, True),
        ]
    )
    return specs
