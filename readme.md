# EUPHORIA

Modular Discord bot on `discord.py`. Features live in cogs (extensions) that you hot load/reload/unload at runtime through a local control channel, no restart needed. Works on Windows and Linux.

## Stack

- Python 3.14
- `discord.py`: bot + slash commands
- `APScheduler` (<4): cron jobs (`AsyncIOScheduler`, tz `Europe/Moscow`)
- `aiomysql` / `PyMySQL`: async MySQL pool

## Setup

```bash
git clone https://github.com/Asguar4/bot-euphoria.git
cd bot-euphoria
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Linux: .venv/bin/python
```

Put a `config.json` in the repo root (gitignored; or pass `--config PATH`, or set `EUPHORIA_CONFIG`):

```json
{
  "bot": { "TOKEN": "your-discord-token" },
  "database": {
    "DB_HOST": "localhost",
    "DB_PORT": 3306,
    "DB_USER": "bot",
    "DB_PASSWORD": "secret",
    "DB_NAME": "euphoria"
  }
}
```

Optional keys (defaults shown):

```json
{
  "bot": { "EXTENSIONS": [], "SHUTDOWN_TIMEOUT": 30, "TIMEZONE": "Europe/Moscow" },
  "database": {
    "DB_CHARSET": "utf8mb4", "DB_AUTOCOMMIT": true, "DB_MINSIZE": 1, "DB_MAXSIZE": 10,
    "DB_POOL_RECYCLE": 3600, "DB_CONNECT_TIMEOUT": 10, "DB_ECHO": false,
    "DB_CLOSE_TIMEOUT": 5, "DB_EAGER_CONNECT": true
  },
  "control": {
    "ENABLED": true, "HOST": "127.0.0.1", "PORT": 48600, "TOKEN": null,
    "ALLOW_REMOTE": false, "STATE_FILE": null, "MAX_CONNECTIONS": 8,
    "IDLE_TIMEOUT": 60, "COMMAND_TIMEOUT": 120
  },
  "logging": { "DIR": null, "LEVEL": "INFO", "CONSOLE": true }
}
```

`DB_AUTOCOMMIT`, `DB_ECHO` and `DB_EAGER_CONNECT` accept a JSON bool or `"true"`/`"false"`. The bot connects to MySQL at startup and fails fast; set `DB_EAGER_CONNECT` to `false` for lazy connection. Relative paths in the config resolve against the config file's directory.

## Run

```bash
python -m euphoria            # same as: python -m euphoria run
python -m euphoria run --config path/to/config.json --log-dir logs --no-control
```

`cd src && python main.py` still works. Logs go to `<repo>/logs/` (`<logger>.log` for the `bot.logger` facade, `euphoria.log` for library and internal logs), rotating at 5 MB x 5.

Exit codes: `0` clean shutdown, `1` config/startup failure, crash, shutdown timeout or forced shutdown, `2` usage error.

## Control channel

The bot serves newline-delimited JSON on `127.0.0.1:48600` (TCP, loopback only by default). Each run has a random token, published together with host/port/pid in `<repo>/.run/control.json` (deleted on shutdown). Use the client:

```bash
python -m euphoria ctl ping
python -m euphoria ctl load modules.music
python -m euphoria ctl reload modules.music
python -m euphoria ctl unload modules.music
python -m euphoria ctl list
python -m euphoria ctl status
python -m euphoria ctl help
python -m euphoria ctl exit
```

Options: `--host`, `--port`, `--token` (or env `EUPHORIA_CONTROL_TOKEN`), `--state-file`, `--timeout`, `--json`. Exit code is 0 on success, 1 otherwise. Binding to a non-loopback host needs `ALLOW_REMOTE: true` and an explicit `TOKEN` of at least 16 characters.

After a successful load/reload/unload the bot re-syncs the slash command tree.

## Shutdown

Ctrl+C, SIGTERM, Ctrl+Break, `ctl exit` and `bot.close()` all run the same teardown once: stop the control server, stop the scheduler, unload extensions (reverse order), close the Discord connection, close the DB pool, then flush logs. Each step has a timeout; a second signal forces exit with code 1.

Windows limitation: closing the console window and `taskkill /F` are not graceful. Use `python -m euphoria ctl exit` to stop a service.

## Writing a module

Extensions are imported relative to `src/`, so `modules.music` means `src/modules/music.py`. `modules/` is gitignored, so modules stay outside the repo. List extensions to load at startup in `bot.EXTENSIONS`.

```python
from discord import app_commands
from discord.ext import commands
from utils.managerPermission import ManagerPermission


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log = bot.logger
        self.db = bot.database

    @app_commands.command(name="ping")
    @ManagerPermission.isOwnerApp()
    async def ping(self, interaction):
        await interaction.response.send_message("pong")


async def setup(bot):
    await bot.add_cog(Music(bot))
```

What a module can use on `bot`:

- `bot.database`: `execute`, `fetchall`, `get/add/set/delete(id, ..., base='activities')`. Table and column names are validated as SQL identifiers. JSON columns get deserialized automatically.
- `bot.add_scheduler_task(func, day, hour, minute, id, args=())`: cron job, `id` is unique (re-adding the same id overwrites the job). `func` must be `async def`; sync jobs run in the default executor and can delay process exit past `SHUTDOWN_TIMEOUT` (a warning is logged).
- `bot.logger` or `LoggerManager().get_logger(name)`: `debug/info/success/warning/error/exception`.
- `bot.request_shutdown(reason)`: non-blocking shutdown request.
- `ManagerPermission`: `isOwner()` (prefix commands), `isOwnerApp()`, `isRole(role_id)` (user's top role >= that role), `isUser(user_id)`.

## Layout

```
src/
├── main.py                  # legacy entrypoint shim
├── euphoria/                # application: cli, bot, app, lifecycle, settings, control/
└── utils/                   # cog-facing API (config, database, managerPermission, moduleManager, loggerManager)
```
