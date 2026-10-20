# Euphoria

Modular Discord bot on `discord.py`. Features live in cogs (extensions) that you hot load/reload/unload at runtime through a FIFO pipe, no restart needed.

## Stack

- Python 3.10+
- `discord.py`: bot + slash commands
- `APScheduler`: cron jobs (`AsyncIOScheduler`, tz `Europe/Moscow`)
- `aiomysql` / `PyMySQL`: async MySQL pool
- Linux only: uses `os.mkfifo`, logs go to `/var/log`

## Setup

```bash
git clone https://github.com/Asguar4/bot-euphoria.git
cd bot-euphoria
python3 -m venv venv && source venv/bin/activate
pip install discord.py APScheduler aiomysql PyMySQL
```

Put a `config.json` in the repo root (it's gitignored):

```json
{
  "bot": { "TOKEN": "your-discord-token" },
  "database": {
    "DB_HOST": "localhost",
    "DB_PORT": 3306,
    "DB_USER": "bot",
    "DB_PASSWORD": "secret",
    "DB_NAME": "euphoria",
    "DB_CHARSET": "utf8mb4",
    "DB_AUTOCOMMIT": "true",
    "DB_MINSIZE": 1,
    "DB_MAXSIZE": 10,
    "DB_POOL_RECYCLE": 3600,
    "DB_CONNECT_TIMEOUT": 10,
    "DB_ECHO": "false"
  }
}
```

`DB_AUTOCOMMIT` and `DB_ECHO` are strings (compared via `.lower() == 'true'`), the rest are ints.

## Run

```bash
cd src
python main.py
```

Run it from `src/`: the config is read from `../config.json` relative to the CWD. You need write access to `/var/log` (root, or chown the files to the bot user). Logs go to `/var/log/<logger>.log`, rotating at 5 MB × 5 backups.

## Control via FIFO

The bot creates `/tmp/fifo-bot` and reads one command per line. Responses go back into the same pipe:

```bash
cat /tmp/fifo-bot                          # read "Bot started successfully" once on boot
echo "load modules.music" > /tmp/fifo-bot
cat /tmp/fifo-bot                          # read the response
```

| Command | Action |
|---|---|
| `load <ext>` | `bot.load_extension(ext)` |
| `reload <ext>` | `bot.reload_extension(ext)` |
| `unload <ext>` | `bot.unload_extension(ext)` |
| `exit` | graceful shutdown (closes DB pool, logs out) |

After every load/reload/unload the bot re-syncs the slash command tree.

The startup message blocks the pipe until someone reads it, so no commands get processed until you `cat` it.

## Writing a module

Extensions are imported relative to `src/`, so `modules.music` means `src/modules/music.py`. `modules/` is gitignored, so modules stay outside the repo.

```python
from discord import app_commands
from discord.ext import commands
from utils.managerPermission import ManagerPermission

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log = bot.logger
        self.db = bot.database

    @app_commands.command(name='ping')
    @ManagerPermission.isOwnerApp()
    async def ping(self, interaction):
        await interaction.response.send_message('pong')

async def setup(bot):
    await bot.add_cog(Music(bot))
```

What a module can use on `bot`:

- `bot.database`: `execute`, `fetchall`, `get/add/set/delete(id, ..., base='activities')`. JSON columns get deserialized automatically.
- `bot.add_scheduler_task(func, day, hour, minute, id, args=())`: cron job, `id` is unique (re-adding the same id overwrites the job).
- `bot.logger` or `LoggerManager().get_logger(name)`: `debug/info/success/warning/error/exception`.
- `ManagerPermission`: `isOwner()` (prefix commands), `isOwnerApp()`, `isRole(role_id)` (user's top role >= that role), `isUser(user_id)`.

A failed check returns an ephemeral "You don't have permission" to the user. Other command errors are sent back to the user as an ephemeral message too.

## Layout

```
src/
├── main.py                  # Bot class + entrypoint
└── utils/
    ├── config.py            # loads ../config.json
    ├── database.py          # aiomysql pool wrapper
    ├── managerPermission.py # app_commands checks
    ├── moduleManager.py     # load/reload/unload extensions
    ├── inputManager/        # FIFO reader + command dispatcher
    └── loggerManager/       # singleton logger, rotating file sinks
```
