"""Command dispatch with arity checks, timeouts and error isolation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence

from euphoria.control.commands import CommandResult, CommandSpec
from euphoria.control.protocol import ErrorCode

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """Routes command names to handlers."""

    def __init__(
        self,
        commands: Iterable[CommandSpec],
        *,
        timeout: float,
        is_shutting_down: Callable[[], bool],
    ) -> None:
        self._commands: dict[str, CommandSpec] = {}
        for spec in commands:
            if spec.name in self._commands:
                raise ValueError(f"duplicate command {spec.name!r}")
            self._commands[spec.name] = spec
        self._timeout = timeout
        self._is_shutting_down = is_shutting_down

    def names(self) -> list[str]:
        """Return sorted command names."""
        return sorted(self._commands)

    async def dispatch(self, name: str, args: Sequence[str]) -> CommandResult:
        """Execute a command and always return a result (never a traceback)."""
        started = time.perf_counter()
        result = await self._dispatch(name, tuple(args))
        logger.info(
            "control command %s %s -> %s (%.0f ms)",
            name,
            list(args),
            result.code,
            (time.perf_counter() - started) * 1000,
        )
        return result

    async def _dispatch(self, name: str, args: tuple[str, ...]) -> CommandResult:
        spec = self._commands.get(name)
        if spec is None:
            return CommandResult(
                False, f"unknown command '{name}'; try 'help'", ErrorCode.UNKNOWN_COMMAND
            )
        if self._is_shutting_down() and not spec.allowed_during_shutdown:
            return CommandResult(False, "bot is shutting down", ErrorCode.SHUTTING_DOWN)
        if not spec.min_args <= len(args) <= spec.max_args:
            return CommandResult(False, f"usage: {spec.usage}", ErrorCode.BAD_ARGUMENTS)
        try:
            async with asyncio.timeout(self._timeout):
                return await spec.handler(args)
        except TimeoutError:
            return CommandResult(
                False, f"command timed out after {self._timeout:g}s", ErrorCode.TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control command %s failed", name)
            return CommandResult(False, "internal error, see logs", ErrorCode.INTERNAL_ERROR)
