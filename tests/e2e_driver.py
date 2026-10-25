"""Subprocess driver: runs the real CLI with Discord's network start replaced.

Usage: python e2e_driver.py [--hang-gateway] <cli args...>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from euphoria import bot as botmod  # noqa: E402
from euphoria import cli  # noqa: E402

args = sys.argv[1:]
hang = False
if args and args[0] == "--hang-gateway":
    hang = True
    args = args[1:]


async def fake_start(self: botmod.Bot, token: str, *, reconnect: bool = True) -> None:
    await self.setup_hook()
    print("READY", flush=True)
    while not self.is_closed():
        await asyncio.sleep(0.05)


async def hang_gateway(self: botmod.Bot) -> None:
    await asyncio.sleep(3600)


botmod.Bot.start = fake_start  # type: ignore[method-assign]
if hang:
    botmod.Bot._close_gateway = hang_gateway  # type: ignore[method-assign]

raise SystemExit(cli.main(args))
