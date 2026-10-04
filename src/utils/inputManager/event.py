class Event:
    _instance = None
    _initialized = False

    def __new__(self):
        if self._instance is None:
            self._instance = super().__new__(self)
        return self._instance

    def __init__(self):
        if not Event._initialized:
            self.running = False
            self.bot = True
            Event._initialized = True
