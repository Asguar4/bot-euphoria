"""Permission checks for commands and app commands."""

from collections.abc import Callable
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


class ManagerPermission:
    """Factory of ``commands``/``app_commands`` checks."""

    @staticmethod
    def isOwner() -> Callable[[Any], Any]:
        """Check that the invoking user owns the bot (prefix commands)."""

        async def predicate(ctx: commands.Context[Any]) -> bool:
            return bool(await ctx.bot.is_owner(ctx.author))

        return commands.check(predicate)

    @staticmethod
    def isOwnerApp() -> Callable[[Any], Any]:
        """Check that the invoking user owns the bot (app commands)."""

        async def predicate(interaction: discord.Interaction) -> bool:
            client: Any = interaction.client
            return bool(await client.is_owner(interaction.user))

        return app_commands.check(predicate)

    @staticmethod
    def isRole(id: int) -> Callable[[Any], Any]:
        """Check that the user's top role is at least the role ``id``.

        Args:
            id: Role id. The check fails (never raises) outside guilds.
        """

        async def predicate(interaction: discord.Interaction) -> bool:
            guild = interaction.guild
            if guild is None:
                return False
            role = guild.get_role(id)
            user = interaction.user
            if role is None or not isinstance(user, discord.Member):
                return False
            return bool(user.top_role >= role)

        return app_commands.check(predicate)

    @staticmethod
    def isUser(id: int) -> Callable[[Any], Any]:
        """Check that the invoking user has the given id.

        Args:
            id: Discord user id.
        """

        async def predicate(interaction: discord.Interaction) -> bool:
            return bool(interaction.user.id == id)

        return app_commands.check(predicate)
