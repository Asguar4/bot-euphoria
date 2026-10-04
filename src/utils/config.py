import json

class Config:
    def __init__(self):
        self.update_config()

    def update_config(self):
        with open('../config.json', 'r', encoding='utf-8') as f:
            self.config = json.loads(f.read())

cfg = Config().config

'''
def _require_env(name: str) -> str:
    """Возвращает значение переменной окружения или бросает исключение."""
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(f'Отсутствует обязательная переменная окружения: {name}')
    return value
'''
