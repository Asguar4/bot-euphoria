import os, discord, asyncio
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.database import Database
from utils.scheduler import scheduler
from utils.loggerManager import LoggerManager
from utils.inputManager.inputManager import InputManager
from utils.moduleManager import ModuleManager
from utils.inputManager.event import Event
from utils.config import cfg

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', help_command=None, intents=discord.Intents.all())
        self.logger = LoggerManager().get_logger('bot')
        self._synced = False

    async def setup_hook(self):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.scheduler.start()
        self.logger.success('Scheduler connected successfully')

        self.database = Database()
        self.logger.success('Database connected successfully')

    def add_scheduler_task(self, func, day: int, hour: int, minute: int, id: str, args:tuple=()):
        if self.scheduler.get_job(job_id=id) is not None:
            self.scheduler.add_job(
                func,
                CronTrigger(day_of_week=day, hour=hour, minute=minute),
                id=id,
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
                args=args
            )

    async def on_app_command_error(self, interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            message = 'You don\'t have permission to use this command'
        else:
            message = f'An error occurred while executing the command:\n{error}'

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name='/help'),
            status=discord.Status.online
        )
        if not self._synced:
            await self.tree.sync()
            self._synced = True
            Event().running = True
            self.logger.success(f'Bot {self.user} started successfully')

    async def close(self):
        self.logger.info('Scheduler stopped')
        await self.database.close()
        self.logger.info('Datebase connection closed')
        await super().close()
        self.logger.info('Bot stopped with exit code 0')
        Event().running = False

async def main():
    bot = Bot()
    module_manager = ModuleManager(bot)
    input_manager = InputManager(module_manager, '/tmp/fifo-bot')

    task = asyncio.create_task(input_manager.read())
    
    try:
        await bot.start(token=cfg['bot']['TOKEN'])
    except Exception as e:
        Event().bot = bot.is_closed()
        print(e)
    
    await task
    if not bot.is_closed():
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
