from abc import ABC, abstractmethod

class ICommand(ABC):
    @abstractmethod
    async def execute(self, args):
        pass

class LoadModuleCommand(ICommand):
    def __init__(self, module_manager):
        self.module_manager: ModuleManager = module_manager

    async def execute(self, args):
        return await self.module_manager.load(args[0])

class ReloadModuleCommand(ICommand):
    def __init__(self, module_manager):
        self.module_manager: ModuleManager = module_manager

    async def execute(self, args):
        return await self.module_manager.reload(args[0])

class UnloadModuleCommand(ICommand):
    def __init__(self, module_manager):
        self.module_manager: ModuleManager = module_manager

    async def execute(self, args):
        return await self.module_manager.unload(args[0])

class ExitModuleCommand(ICommand):
    def __init__(self, module_manager):
        self.module_manager: ModuleManager = module_manager

    async def execute(self, args):
        await self.module_manager.exit()
        return 'Bot stopped successfully'
