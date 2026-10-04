import traceback
from discord.ext import commands

class ModuleManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _manage(self, action, name: str, success_message: str, error_message: str):
        result = success_message
        try:
            await action(name)
            self.bot.logger.success(f'ModuleManager: {success_message}')
        except commands.ExtensionError as e:
            self.bot.logger.error(f'ModuleManager: {e} | Traceback: {traceback.format_exc()}')
            result = e
        await self.bot.tree.sync()
        return result

    async def load(self, name: str):
        return await self._manage(
                self.bot.load_extension,
                name,
                f'{name} is loaded',
                f'failed to load {name}'
            )

    async def unload(self, name: str):
        return await self._manage(
            self.bot.unload_extension,
            name,
            f'{name} is unloaded',
            f'failed to unload {name}'
        )

    async def reload(self, name: str):
        return await self._manage(
            self.bot.reload_extension,
            name,
            f'{name} is reloaded',
            f'failed to reload {name}'
        )

    async def exit(self):
        self.bot.logger.info('ModuleManager: executing bot shutdown command')
        await self.bot.close()
