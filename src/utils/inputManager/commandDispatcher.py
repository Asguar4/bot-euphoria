from .iCommand import *
from utils.loggerManager import LoggerManager

class CommandDispatcher:
    def __init__(self, module_manager):
        self.commands = {
            'load': LoadModuleCommand(module_manager),
            'reload': ReloadModuleCommand(module_manager),
            'unload': UnloadModuleCommand(module_manager),
            'exit': ExitModuleCommand(module_manager),
        }

    async def execute(self, command):
        if (command[0] not in self.commands):
            return f'command {command[0]} isn\'t found'
        return await self.commands[command[0]].execute(command[1:])
