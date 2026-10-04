import discord
from discord import app_commands
from discord.ext import commands

class ManagerPermission:

    @staticmethod
    def isOwner():
        async def predicate(ctx: commands.Context) -> bool:
            if await ctx.bot.is_owner(ctx.author):
                return True
            return False
        return commands.check(predicate)

    @staticmethod
    def isOwnerApp():
        async def predicate(interaction: discord.Interaction) -> bool:
            if await interaction.client.is_owner(interaction.user):
                return True
            return False
        return app_commands.check(predicate)

    @staticmethod
    def isRole(id: int):
        async def predicate(interaction: discord.Interaction) -> bool:
            role = interaction.guild.get_role(id)
            if interaction.user.top_role >= role:
                return True
            return False
        return app_commands.check(predicate)

    @staticmethod
    def isUser(id: int):
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.user.id == id:
                return True
            return False
        return app_commands.check(predicate)
