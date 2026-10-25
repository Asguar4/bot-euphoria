# DESIGN: bot-euphoria refactor (graceful shutdown + socket control channel)

## 1. Goal

Refactor the existing `discord.py` bot in `C:\Work\bot-euphoria` so it is correct, cross-platform (Windows 11 primary, Linux secondary) and shuts down cleanly. That means fixing the bugs, races, resource leaks and design smells listed in section 3. The public API that cogs use (`utils.database`, `utils.managerPermission`, `utils.moduleManager`, `utils.config`, `utils.loggerManager`, and the attributes/methods of the `bot` instance) is **frozen**: only implementations change. The Linux-only FIFO console (`utils/inputManager`) is replaced by a token-authenticated, newline-delimited-JSON control protocol over loopback TCP, served from the bot's asyncio event loop. A small synchronous client (`python -m euphoria ctl <command>`) sends commands. Shutdown is a single idempotent, ordered and time-bounded teardown that runs for every stop path: SIGINT/SIGTERM/SIGBREAK, `ctl exit`, a cog calling `bot.close()`, and a startup failure.

## 2. Assumptions & open questions

| # | Question | Assumption taken |
|---|---|---|
| A1 | Do any cogs import `utils.inputManager.*` (e.g. the `Event` singleton)? The README does not document it as cog API. | **No.** The `utils/inputManager/` package is deleted. If an external cog depends on it, a shim must be added. This is open question **Q1**. |
| A2 | Should the bot connect to MySQL eagerly at startup? Today it logs "Database connected successfully" without connecting. | **Yes.** `setup_hook` calls `await database.get_pool()`. If the DB is unreachable, startup fails with exit code 1 and a clear log line. This is open question **Q2**. Lazy connection can be restored with `"database": {"DB_EAGER_CONNECT": false}`. |
| A3 | Transport: Unix socket vs TCP. | **TCP on 127.0.0.1.** asyncio has no `start_unix_server` on Windows (Proactor loop), and Windows is the target. |
| A4 | Who may send control commands? | Any local process that knows the per-run token. The token is random per run unless configured and is published in a state file inside the project directory (`.run/control.json`). Binding to a non-loopback address requires an explicit `ALLOW_REMOTE: true` plus a configured token. |
| A5 | Does `ModuleManager.exit()` still have to block until the bot is fully closed? | **No.** It now *requests* shutdown and returns immediately. Blocking would deadlock, because teardown waits for the control connection that is executing `exit`. Nothing in the documented cog API awaits `exit()`. |
| A6 | What do `load`/`reload`/`unload` return on failure? | A `str` in all cases. Old code returned the `ExtensionError` object on failure. The only consumer was the dispatcher, which stringified it, so for that consumer the change is invisible. |
| A7 | Log location. | Default `<project_root>/logs/` (already gitignored). Override with config `logging.DIR` or `--log-dir`. `/var/log` is no longer hard-coded. |
| A8 | Config location. | `--config PATH`, then env `EUPHORIA_CONFIG`, then `<project_root>/config.json`. The last one equals the old `../config.json` when CWD is `src/`, so old behaviour is preserved. |
| A9 | Python version. | 3.14 (STACK). discord.py 2.7.1, APScheduler 3.11.3, aiomysql and PyMySQL are installed system-wide and import fine on 3.14. |
| A10 | Should the extension root stay `src/` (cogs are `modules.<name>` = `src/modules/<name>.py`)? | **Yes.** The `run` command makes sure `src/` (configurable with `--extensions-root`) is on `sys.path`. |
| A11 | Auto-load extensions at startup? | New optional config key `bot.EXTENSIONS: list[str]` (default `[]`). Without it, behaviour is unchanged. |

`<project_root>` always means `Path(__file__).resolve()` walked up to the repository root (the directory that contains `src/`). It is computed in one helper, `utils/_paths.py::project_root()`.

## 3. Audit of the current code

Line numbers refer to commit `d8cc971`.

### 3.1 `src/main.py`
| Line | Severity | Finding |
|---|---|---|
| 1 | low | `import os` is unused. `import os, discord, asyncio` puts several imports on one line. |
| 22-27 | high | `scheduler` and `database` are created in `setup_hook`, not `__init__`. If login fails before `setup_hook`, `close()` raises `AttributeError: database` at L67. `add_scheduler_task` called before `setup_hook` fails (swallowed at L41-42), so the job is silently lost. |
| 26-27 | medium | Logs "Database connected successfully" without connecting: `Database()` is lazy. |
| 41-42 | low | Logs only `str(e)` with no traceback. |
| 44-53 | medium | App-command errors are never logged. `followup.send`/`send_message` can raise (`NotFound` for expired interactions, `HTTPException`), and that error escapes the error handler. `CommandInvokeError` is not unwrapped. |
| 60-64 | medium | `tree.sync()` in `on_ready` is unguarded. An `HTTPException` there means `Event().running` is never set, so the FIFO reader waits forever and control commands never become available. |
| 66-71 | **high** | Wrong teardown order: the DB pool closes **before** the gateway, cogs and scheduler stop, so in-flight handlers and scheduled jobs hit a closed pool (which `get_pool` then silently **re-creates**, leaking a new pool). The scheduler is never shut down. Cogs are unloaded only implicitly, after the DB is gone. The method is not idempotent: it runs the DB close and logs again on every call. It logs "exit code 0" unconditionally. Typo "Datebase". |
| 73-91 | **high** | Lifecycle: a `print(e)` swallows startup errors and the process always exits 0. `Event().bot = bot.is_closed()` stores inverted, confusing state. `await task` can block forever: the reader thread is stuck in a blocking FIFO `open()` (see 3.4). |
| 90-91 | **high** | No signal handling. On Ctrl+C, `asyncio.run` cancels `main()`. `bot.start` is cancelled without `close()` (unclosed aiohttp session, websocket and pool). `asyncio.run` then waits forever in `shutdown_default_executor()` for the `to_thread` worker that is blocked on the FIFO, so the process hangs. |
| 16 | info | `Intents.all()` requires privileged intents. Kept as is, but `PrivilegedIntentsRequired` must surface as a clear fatal error. |

### 3.2 `src/utils/database.py`
| Line | Severity | Finding |
|---|---|---|
| 6, 18-30 | high | Config is read at import time through `utils.config.cfg`. Importing the module requires `../config.json` relative to CWD, so it cannot be tested or imported from another CWD. Missing keys give a bare `KeyError`. `.lower()` on `DB_AUTOCOMMIT`/`DB_ECHO` crashes if the JSON holds booleans. |
| 14-17 | **high** | **Race:** concurrent first callers of `get_pool()` each create a pool. One leaks with up to `maxsize` open connections. |
| 34, 44, 46 | medium | `print` used for diagnostics. |
| 38-47 | **high** | `close()` has no timeout: `wait_closed()` waits for every acquired connection, so one stuck query hangs shutdown forever. After `close()`, any later call re-creates a pool (resurrection after shutdown). |
| 64 | medium | Mutable default `info: json = {}`. The annotation `json` is a module, not a type. |
| 69-75, 82, 91-94, 101 | **high** | **SQL injection through identifiers**: `base` and dict keys (column names) are f-string interpolated without validation or quoting. |
| 75, 94 | **high** | `add` and `set` never `commit()`, unlike `execute`/`delete`. With `DB_AUTOCOMMIT=false`, writes are silently lost. |
| 86-94 | low | `set` with an empty `values` builds invalid SQL (`SET  WHERE`). `(tuple(vals))` has redundant parentheses. |
| all writes | medium | No rollback on error. With autocommit off, a failed write can return a connection to the pool mid-transaction. |

### 3.3 `src/utils/config.py`
| Line | Severity | Finding |
|---|---|---|
| 8 | high | CWD-relative path `'../config.json'`. Breaks under any other CWD (tests, services, `python -m`). |
| 11 | high | Import-time file I/O: `cfg = Config().config`. |
| 7-9 | medium | `update_config()` rebinds `self.config` to a new dict, so the module-level `cfg` never sees the reload. Every `Config()` re-reads the file. |
| 13-20 | low | Dead code in a string literal. |
| 1 | low | UTF-8 BOM (also in `database.py`, `managerPermission.py`, `inputManager.py`, `utils/__init__.py`). Mixed CRLF. |

### 3.4 `src/utils/inputManager/*` (to be replaced)
| File:Line | Severity | Finding |
|---|---|---|
| inputManager.py:16 | **high** | `os.mkfifo` is Linux-only. The project cannot run on Windows. |
| inputManager.py:16 | **high/security** | FIFO mode `0o666` in `/tmp`: **any local user can load arbitrary Python modules into the bot** (code execution as the bot user). |
| inputManager.py:18-24, 33 | **high** | The same FIFO carries both requests and responses. Each `open()` blocks until a peer opens the other end. The startup message blocks the command loop until someone `cat`s it. If nobody reads a response, the loop hangs forever. The bot may also read back its own response as a command. |
| inputManager.py:30-31 | medium | Polls the `Event` singleton with `sleep(1)` instead of waiting on an event. |
| inputManager.py:38, 40 | **high** | Blocking reads in `asyncio.to_thread` cannot be cancelled, so the process hangs on shutdown (see 3.1 L90-91). |
| inputManager.py:41-42 | medium | `except OSError: self._creat()` turns persistent errors into a hot busy loop. |
| inputManager.py:43-44 | medium | Full tracebacks are written to a world-readable pipe (information leak). |
| inputManager.py:47 | high | On a startup error, writing "Exit" blocks forever until someone reads it. |
| inputManager.py:48 | low | `os.unlink` raises if the file is already gone. |
| commandDispatcher.py:14 | medium | An empty line gives `IndexError` on `command[0]`. |
| commandDispatcher.py:15 | low | Leftover debug `print(command)`. |
| commandDispatcher.py:1 | low | `from .iCommand import *`. |
| iCommand.py:10, 17, 24, 31 | low | `ModuleManager` is an undefined name in annotations (F821). `args[0]` with no args gives `IndexError`, which ends up as a traceback. |
| iCommand.py:33-35 | high | `exit` awaits `bot.close()` from inside the reader task, then tries to write the response into a pipe nobody may read. |
| event.py | medium | Global mutable singleton used as a cross-task flag (`running`, `bot`) with ambiguous semantics. |

### 3.5 `src/utils/loggerManager/*`
| File:Line | Severity | Finding |
|---|---|---|
| loggerManager.py:5-7 | high | Hard-coded `/var/log` plus `makedirs` at **import time**: needs root on Linux, ends up as `C:\var\log` on Windows, and is a side effect on import. |
| \_\_init\_\_.py:8 | medium | `__all__` lists `"ConsoleSink"`, which does not exist, so `from utils.loggerManager import *` raises `AttributeError`. |
| logSink.py:52 | **high** | Calls `handler.emit()` directly, which bypasses `Handler.handle()` and therefore **its lock**. Concurrent writes from threads (executors, libraries) race on `RotatingFileHandler` rollover. On Windows a rollover while the file is open elsewhere raises `PermissionError`. |
| logSink.py:43 | low | Record name is always `"Logger"` and `pathname` is the sink file itself, so the information is useless. |
| logSink.py:24 | low | `os.makedirs`/`os.path` instead of `pathlib`. |
| logger.py:14-18 | low | Sink failures are swallowed with no trace at all. |
| loggerManager.py:14-22 | low | Singleton is not thread-safe. There is no way to close all sinks at shutdown, so handlers are never flushed or closed explicitly. |
| main.py (global) | medium | Library logs (`discord`, `apscheduler`, `aiomysql`) go nowhere: `bot.start()` does not call `discord.utils.setup_logging`. |

### 3.6 `src/utils/managerPermission.py`
| Line | Severity | Finding |
|---|---|---|
| 26 | high | `interaction.guild` is `None` in DMs, which raises `AttributeError` inside the check. That becomes a generic error instead of a permission denial. |
| 26-27 | high | `get_role(id)` may return `None`, so `top_role >= None` raises `TypeError`. In DMs, `interaction.user` is a `User` without `top_role`. |
| 9-13, 17-21, 34-37 | low | `if x: return True; return False` instead of `return x`. |

### 3.7 `src/utils/moduleManager.py`
| Line | Severity | Finding |
|---|---|---|
| 8 | low | `error_message` parameter is unused. |
| 15 | medium | Returns the exception object instead of `str`. |
| 14 | low | Logs with a manual `traceback.format_exc()`. |
| 16 | **high** | `tree.sync()` runs after **every** call, including failures. It is unguarded (an `HTTPException` or rate limit propagates to the caller). Syncing on failure wastes rate-limited sync quota. |
| 8-17 | medium | Concurrent load/reload/unload of extensions is not serialised. |
| 43-45 | high | `exit()` awaits `bot.close()` from inside a command handler (see 3.4). |

### 3.8 Project-level
- No `pyproject.toml`, no tests, no lint/type configuration.
- `src/__init__.py` turns `src` into a package (wrong for a src layout; risks double imports `src.utils` vs `utils`).
- `.gitignore` ignores `*.json` globally, which would hide test fixtures. It lacks `.venv/` and `.run/`.
- The README states "Linux only" and documents the FIFO.

## 4. Stack deviations

| # | Deviation | Justification |
|---|---|---|
| D1 | **asyncio-based runtime** (STACK forbids "async frameworks") | `discord.py` is asyncio-only. The whole app (gateway, cogs, DB pool, scheduler, control server) must share one event loop. No new async framework is introduced; only stdlib `asyncio` plus the existing dependencies. |
| D2 | **`discord.py` >= 2.4** (runtime) | Core of the existing product. Cogs depend on `discord`, `discord.ext.commands` and `discord.app_commands`. |
| D3 | **`aiomysql` >= 0.2 and `PyMySQL` >= 1.1** (runtime, DB driver) | Existing DB layer, frozen API. `pymysql.converters`/`FIELD_TYPE` are used for the JSON converter. Not an ORM. |
| D4 | **`APScheduler` >= 3.10, < 4** (runtime) | `bot.add_scheduler_task` is frozen cog API built on `AsyncIOScheduler`/`CronTrigger`. **Upper bound `<4`** breaks the "`>=` only" rule because APScheduler 4 is an incompatible rewrite (no `AsyncIOScheduler`). |
| D5 | **Package layout:** `src/utils/` stays a top-level package with camelCase module names (`managerPermission`, `moduleManager`, `loggerManager`), next to the new `src/euphoria/` package | Cogs import `from utils.managerPermission import ManagerPermission` and similar. Moving or renaming would break every cog. New application code goes into `src/euphoria/` following STACK. |
| D6 | **Global mutable state kept:** the `LoggerManager` singleton and module-level `utils.config.cfg` | Both are frozen cog API. Internals are made thread-safe and lazy. New code passes settings and dependencies explicitly. |
| D7 | **Two logging channels** (the frozen `Logger`/`LogSink` facade plus stdlib `logging`) | The `Logger` facade (`.success()`, sinks, per-name files) is frozen cog API. New internal code follows STACK (`logging.getLogger(__name__)`). Both write under the same log directory. |
| D8 | No `pytest-asyncio` | Not on the approved list. Async tests use `asyncio.run(...)` inside sync tests, or `unittest.IsolatedAsyncioTestCase`. This is not a deviation, but it is noted so nobody adds the plugin. |

## 5. Frozen public API (must remain call-compatible)

**Compatibility rule.** For every item below, the following must stay identical: the name, the location (import path), and the parameter **names, order, kinds and default behaviour**. Allowed changes are limited to these:
1. Type annotations may be corrected or narrowed.
2. Return values may change only where marked "changed" below.
3. **Additive** keyword-only optional parameters, methods and module attributes may be added, but only where this section lists them (marked "new").

The tester verifies this with `inspect.signature` (test file `tests/test_frozen_api.py`).

### 5.1 `utils.config`
| Symbol | Signature / shape | Notes |
|---|---|---|
| `cfg` | module attribute, `dict[str, Any]` | `from utils.config import cfg` must work. **Now lazy** through module `__getattr__` (PEP 562): the file is read on first access, not at import. Always the same dict object; reloads update it in place. |
| `Config` | `class Config: def __init__(self) -> None` | `Config()` returns the shared instance (singleton). Construction loads the config if it is not loaded yet. |
| `Config.config` | instance attribute `dict[str, Any]` | The same object as `cfg`. |
| `Config.update_config` | `def update_config(self) -> None` | Re-reads the file and updates `self.config` **in place** (`clear()` + `update()`), so `cfg` reflects the reload. Raises `ConfigError` on invalid JSON or a missing file, and leaves the old content untouched in that case. |
| new: `ConfigError` | `class ConfigError(Exception)` | |
| new: `set_config_path` | `def set_config_path(path: Path) -> None` | Must be called before the first `cfg` access (the CLI does this). **Rev 2:** if the config is not loaded yet, it only stores the path (no I/O). If it is already loaded, it reloads **eagerly** under the lock: reads the new file, and on success sets the path and updates the shared dict in place (`clear()` + `update()`), so `cfg` is never observed empty. On `ConfigError` it re-raises and keeps both the old path and the old content. The shared dict is never marked unloaded. |
| new: `get_config_path` | `def get_config_path() -> Path` | Resolution order: explicit path, then env `EUPHORIA_CONFIG`, then `project_root()/"config.json"`. |

### 5.2 `utils.database`
| Symbol | Signature | Notes |
|---|---|---|
| `Database` | `def __init__(self, *, settings: DatabaseSettings \| None = None) -> None` | `Database()` still works. The new kw-only `settings` is for tests and DI. When `None`, settings are built lazily from `cfg["database"]` on the first `get_pool()`. No I/O in `__init__`. |
| `Database.converter` | instance attribute, `dict[Any, Any]` | Copy of `pymysql.converters.conversions` with `FIELD_TYPE.JSON -> json.loads`. |
| `Database.get_pool` | `async def get_pool(self) -> aiomysql.Pool` | Race-free (an `asyncio.Lock` with double-checked `_pool`). Raises `RuntimeError("Database is closed")` after `close()`. |
| `Database.close` | `async def close(self) -> None` | Idempotent. Bounded by `DB_CLOSE_TIMEOUT` (default 5 s), then `pool.terminate()`. Never raises: errors are logged. |
| `Database.execute` | `async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int` | Returns `cursor.lastrowid`. Commits. |
| `Database.fetchall` | `async def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]` | Rows as dicts (DictCursor). JSON columns are decoded. |
| `Database.add` | `async def add(self, id: int, info: Mapping[str, Any] \| None = None, base: str = 'activities') -> None` | Default changed from `{}` to `None`, which behaves the same (empty mapping). Commits. |
| `Database.get` | `async def get(self, id: int, base: str = 'activities') -> dict[str, Any] \| None` | |
| `Database.set` | `async def set(self, id: int, values: Mapping[str, Any], base: str = 'activities') -> None` | Commits. **Changed:** empty `values` now raises `ValueError` (it used to fail with a MySQL syntax error). |
| `Database.delete` | `async def delete(self, id: int, base: str = 'activities') -> None` | Commits. |
| new: `DatabaseSettings` | frozen dataclass, see 7.3 | |
| Behaviour change (all of `add/get/set/delete`) | | `base` and column names are validated as SQL identifiers. Invalid ones raise `ValueError` **before** any DB access. Valid ones are back-quoted. |

### 5.3 `utils.managerPermission`
| Symbol | Signature |
|---|---|
| `ManagerPermission.isOwner` | `@staticmethod def isOwner() -> Callable[[T], T]`, returns `commands.check(...)`. Predicate: `await ctx.bot.is_owner(ctx.author)`. |
| `ManagerPermission.isOwnerApp` | `@staticmethod def isOwnerApp() -> Callable[[T], T]`, returns `app_commands.check(...)`. |
| `ManagerPermission.isRole` | `@staticmethod def isRole(id: int) -> Callable[[T], T]`. Keep the param name `id`. Predicate returns `False` (never raises) when: no guild, role not found, or user is not a `discord.Member`. Otherwise `user.top_role >= role`. |
| `ManagerPermission.isUser` | `@staticmethod def isUser(id: int) -> Callable[[T], T]`. Predicate: `interaction.user.id == id`. |

### 5.4 `utils.moduleManager`
| Symbol | Signature | Notes |
|---|---|---|
| `ModuleManager` | `def __init__(self, bot: commands.Bot) -> None` | Attribute `bot` kept. |
| `load` / `unload` / `reload` | `async def load(self, name: str) -> str` (same for the other two) | **Changed:** always returns `str`. Success messages are unchanged (`"<name> is loaded"`, `"<name> is unloaded"`, `"<name> is reloaded"`). Failure message: `"failed to load <name>: <error>"` (similarly for unload/reload). |
| `exit` | `async def exit(self) -> None` | **Changed semantics:** *requests* shutdown (`bot.request_shutdown("module manager exit")` if the bot has that method, else `await bot.close()`) and returns. |
| new: `run` | `async def run(self, action: ModuleAction, name: str) -> ModuleOpResult` | Used by the control commands. See 7.5. |

### 5.5 `utils.loggerManager`
| Symbol | Signature | Notes |
|---|---|---|
| `__all__` | `["LogSink", "ConsoleSink", "FileSink", "Logger", "LoggerManager"]` | `ConsoleSink` now exists. |
| `LogSink` | `class LogSink: def write(self, level: str, message: str) -> None`; new `def close(self) -> None` (no-op) | Plain base class, not an ABC. |
| `FileSink` | `def __init__(self, file_path: str \| os.PathLike[str]) -> None`; `write(level, message)`; `close()`; attribute `file_path` | |
| new: `ConsoleSink` | `def __init__(self, stream: TextIO \| None = None) -> None` (default `sys.stderr`); `write`; `close` (flushes, never closes stderr) | |
| `Logger` | `def __init__(self) -> None`; attribute `sinks: list[LogSink]`; methods `debug/info/success/warning/error/exception(self, message: str) -> None` | |
| `LoggerManager` | `def __init__(self) -> None` (singleton); attribute `loggers: dict[str, Logger]`; `create_logger(name) -> Logger`; `get_logger(name) -> Logger`; `remove_logger(name) -> None` | |
| new: `LoggerManager.configure` | `def configure(self, log_dir: Path \| None = None, *, console: bool \| None = None) -> None` | Affects loggers created **after** the call. |
| new: `LoggerManager.shutdown` | `def shutdown(self) -> None` | Closes all sinks of all loggers and clears `loggers`. Idempotent. |
| new: `LoggerManager.log_dir` | property, `-> Path` | |

### 5.6 The `bot` instance (cogs receive it as `self.bot`)
| Member | Signature / type | Notes |
|---|---|---|
| class | `euphoria.bot.Bot(commands.Bot)`, re-exported as `main.Bot` by the shim | |
| `bot.logger` | `utils.loggerManager.Logger`, the logger named `"bot"` | Available from `__init__`. |
| `bot.database` | `utils.database.Database` | Now available from `__init__` (it used to appear only after `setup_hook`). |
| `bot.scheduler` | `apscheduler.schedulers.asyncio.AsyncIOScheduler`, timezone `Europe/Moscow` | Created in `__init__`, started in `setup_hook`. |
| `bot.add_scheduler_task` | `def add_scheduler_task(self, func: Callable[..., Any], day: int \| str, hour: int \| str, minute: int \| str, id: str, args: tuple[Any, ...] = ()) -> None` | Same CronTrigger kwargs as before (`replace_existing=True, misfire_grace_time=3600, coalesce=True`). Still never raises: it logs with a traceback. Works before `setup_hook` (the job is stored as pending). |
| `bot.on_app_command_error` | `async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None` | User-visible texts unchanged. |
| `bot.close` | `async def close(self) -> None` | Idempotent ordered teardown (section 9). |
| new: `bot.request_shutdown` | `def request_shutdown(self, reason: str) -> None` | Non-blocking. Safe to call from cogs. |
| new: `bot.module_manager` | `ModuleManager` | |
| `Bot()` | zero-arg construction still works (settings loaded from `cfg`) | Signature: `def __init__(self, settings: AppSettings \| None = None, *, database: Database \| None = None, shutdown: ShutdownController \| None = None) -> None` |

### 5.7 Removed
`utils.inputManager` (the whole package: `InputManager`, `CommandDispatcher`, `ICommand`, `*ModuleCommand`, `Event`). It is not cog API (A1, Q1).

## 6. Inputs / outputs

### 6.1 Config file (`config.json`, UTF-8)
The existing keys are unchanged. New keys are all **optional**:
```json
{
  "bot": {
    "TOKEN": "required",
    "EXTENSIONS": ["modules.music"],
    "SHUTDOWN_TIMEOUT": 30,
    "TIMEZONE": "Europe/Moscow"
  },
  "database": {
    "...existing DB_* keys...": "",
    "DB_CLOSE_TIMEOUT": 5,
    "DB_EAGER_CONNECT": true
  },
  "control": {
    "ENABLED": true,
    "HOST": "127.0.0.1",
    "PORT": 48600,
    "TOKEN": null,
    "ALLOW_REMOTE": false,
    "STATE_FILE": null,
    "MAX_CONNECTIONS": 8,
    "IDLE_TIMEOUT": 60,
    "COMMAND_TIMEOUT": 120
  },
  "logging": { "DIR": null, "LEVEL": "INFO", "CONSOLE": true }
}
```
- `DB_AUTOCOMMIT`, `DB_ECHO` and `DB_EAGER_CONNECT` accept a JSON bool **or** the strings `"true"`/`"false"` (case-insensitive). Any other value raises `ConfigError`.
- `PORT` is 0..65535. `0` means an ephemeral port, discoverable through the state file.
- `STATE_FILE` null means `project_root()/".run"/"control.json"`. `logging.DIR` null means `project_root()/"logs"`.
- Relative paths in the config resolve against the directory of the config file.

### 6.2 CLI (`python -m euphoria`, argparse)
```
python -m euphoria [run] [--config PATH] [--log-dir PATH] [--control-host HOST]
                         [--control-port PORT] [--no-control] [--no-console-log]
                         [--extensions-root PATH]
python -m euphoria ctl [--host HOST] [--port PORT] [--token TOKEN] [--state-file PATH]
                       [--timeout SECONDS] [--json] COMMAND [ARG ...]
```
- With no subcommand, `run` is used (`python -m euphoria` starts the bot).
- `ctl` token precedence: `--token`, then env `EUPHORIA_CONTROL_TOKEN`, then the state file. Host/port precedence: flags, then the state file, then defaults `127.0.0.1:48600`. `--timeout` defaults to 130 s (longer than the server's command timeout). `--json` prints the raw response JSON to stdout.
- `ctl` output: on success, the `message` goes to stdout, plus `data` rendered one item per line when it is a list or `key: value` lines when it is a dict. On failure, `error [<code>]: <message>` goes to stderr.
- Legacy entrypoint: `cd src && python main.py` still works (thin shim calling `euphoria.cli.main(["run"])`).

### 6.3 Exit codes
| Code | `run` | `ctl` |
|---|---|---|
| 0 | Clean shutdown after a signal, `ctl exit`, or `bot.close()` from a cog, completed within `SHUTDOWN_TIMEOUT` | Server replied `ok: true` |
| 1 | Config error, startup failure (login, intents, DB, control port in use), unexpected crash, teardown timeout, or forced shutdown (second signal) | Connection error, timeout, protocol error, or server replied `ok: false` |
| 2 | argparse usage error | argparse usage error |

### 6.4 Files written
- `<log_dir>/<logger-name>.log` (frozen `Logger` facade, e.g. `bot.log`): rotating, 5 MB x 5, UTF-8.
- `<log_dir>/euphoria.log`: stdlib root logging (internals plus `discord`, `apscheduler`, `aiomysql`): rotating, 5 MB x 5, UTF-8.
- `<state_file>` (`.run/control.json`): `{"host": str, "port": int, "token": str, "pid": int, "started_at": ISO-8601 str}`. Written atomically (tmp file then `Path.replace`). On POSIX the directory gets `0o700` and the file `0o600`. Deleted at teardown, but only if its `pid` equals ours.

## 7. Module & file layout

```
pyproject.toml                     NEW
readme.md                          UPDATED (Windows, ctl usage, config keys, shutdown)
.gitignore                         UPDATED (+ .venv/, .run/, !tests/fixtures/*.json)
src/
  main.py                          REWRITTEN as a 5-line shim: re-export Bot, call cli.main(["run"])
  __init__.py                      DELETED (src is not a package)
  euphoria/                        NEW application package
    __init__.py                    __version__
    __main__.py                    raise SystemExit(cli.main())
    cli.py                         argparse, main(), run/ctl handlers, logging setup
    settings.py                    AppSettings/BotSettings/ControlSettings/LoggingSettings + load_settings
    bot.py                         Bot(commands.Bot), ordered teardown
    app.py                         run_app() coroutine: start bot, wait for shutdown, teardown, exit code
    lifecycle.py                   ShutdownController, install_signal_handlers
    control/
      __init__.py
      protocol.py                  wire format: Request/Response, parse/encode, ErrorCode
      commands.py                  CommandSpec, CommandResult, build_default_commands()
      dispatcher.py                CommandDispatcher
      server.py                    ControlServer (asyncio TCP server, state file)
      client.py                    sync socket client + run_ctl()
  utils/                           FROZEN API, implementation fixed
    __init__.py                    (BOM removed, empty)
    _paths.py                      NEW: project_root() -> Path
    config.py
    database.py
    managerPermission.py
    moduleManager.py
    loggerManager/{__init__,logSink,logger,loggerManager}.py
    inputManager/                  DELETED
tests/
  conftest.py
  fixtures/config_valid.json, config_minimal.json, config_bad_types.json
  test_frozen_api.py  test_config.py  test_database.py  test_logger.py
  test_permission.py  test_module_manager.py  test_protocol.py  test_dispatcher.py
  test_control_server.py  test_client.py  test_lifecycle.py  test_bot_teardown.py
  test_app.py  test_cli.py
```
All files are saved as UTF-8 **without BOM** with LF line endings (ruff format normalises).

## 8. Components

The developer must not deviate from the names and signatures below. Every public function and class gets a Google-style docstring.

### 8.1 `utils/_paths.py`
- `def project_root() -> Path`: returns `Path(__file__).resolve().parents[2]`, the repository root. Pure.

### 8.2 `utils/config.py`
- Module private state: `_explicit_path: Path | None`, `_shared: Config | None`, and a `threading.Lock`.
- `def _read_json(path: Path) -> dict[str, Any]`: reads with `encoding="utf-8"` (also accepts a BOM via `utf-8-sig`). Raises `ConfigError` (message includes the path) on `OSError`, `json.JSONDecodeError`, or when the top level is not an object.
- `class Config`: singleton through `__new__`. `__init__` loads only once. `update_config()` behaves as in 5.1.
- `def __getattr__(name: str) -> Any`: for `"cfg"` returns `Config().config`. Anything else raises `AttributeError`.
- `set_config_path`, `get_config_path`, `ConfigError` as in 5.1. Rev 2: `set_config_path` after loading reuses the same read-then-swap path as `update_config()` (read first, mutate only on success); there is no "reset to unloaded" state.

### 8.3 `utils/database.py`
- `@dataclass(frozen=True, slots=True) class DatabaseSettings`: `host: str; port: int; user: str; password: str; db: str; charset: str = "utf8mb4"; autocommit: bool = True; minsize: int = 1; maxsize: int = 10; pool_recycle: int = 3600; connect_timeout: int = 10; echo: bool = False; close_timeout: float = 5.0; eager_connect: bool = True`.
  - `@classmethod def from_mapping(cls, data: Mapping[str, Any]) -> DatabaseSettings`: maps the `DB_*` keys. Required: `DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME`. Booleans parsed through `_parse_bool`. Raises `ConfigError` naming the offending key on missing keys or wrong types. `minsize <= maxsize` is enforced.
  - `__repr__` must not show the password (`field(repr=False)`).
- `def _parse_bool(value: object, key: str) -> bool`.
- `_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")`
- `def quote_identifier(name: str) -> str`: accepts `ident` or `schema.ident` (at most one dot). Each part must `fullmatch` the regex, otherwise `ValueError(f"invalid SQL identifier: {name!r}")`. Returns the back-quoted form, e.g. `` `schema`.`table` ``. Pure.
- `class Database`: per 5.2. Private: `_settings: DatabaseSettings | None`, `_pool: aiomysql.Pool | None`, `_lock: asyncio.Lock`, `_closed: bool`.
  - `async def _write(self, query: str, params: Sequence[Any]) -> int`: acquire, then cursor, then execute, then `commit()`. On any exception: best-effort `rollback()` (errors suppressed and logged), then re-raise. Returns `lastrowid`. Used by `execute/add/set/delete`.
  - `add`: columns = `["id", *info.keys()]`, all quoted. Placeholders `%s`.
  - `set`: raises `ValueError` if `values` is empty.
  - `close`: set `_closed = True` **first**, so no pool can be re-created. Then `pool.close()` and `async with asyncio.timeout(close_timeout): await pool.wait_closed()`. On `TimeoutError`: log a warning, `pool.terminate()`, then `await pool.wait_closed()` bounded by 1 s. Finally `_pool = None`. Never raises.
  - Logging: `logger = logging.getLogger(__name__)`. No `print`.

### 8.4 `utils/managerPermission.py`
As in 5.3. Predicates are `async def predicate(...) -> bool`. `isRole` uses `isinstance(interaction.user, discord.Member)`.

### 8.5 `utils/moduleManager.py`
- `type ModuleAction = Literal["load", "reload", "unload"]`
- `@dataclass(frozen=True, slots=True) class ModuleOpResult: ok: bool; message: str; synced: bool`
- `class ModuleManager`:
  - `__init__(bot)`: stores `bot`, `self._lock = asyncio.Lock()`, `self._log = LoggerManager().get_logger("bot")` (the same object as `bot.logger`).
  - `async def run(self, action: ModuleAction, name: str) -> ModuleOpResult`: under `_lock`, calls `bot.load_extension` / `reload_extension` / `unload_extension`.
    - On `commands.ExtensionError`: log via `self._log.exception(...)` and return `ok=False`, message `"failed to <action> <name>: <e>"`, `synced=False`.
    - On success, `await bot.tree.sync()`. If sync raises `discord.HTTPException` (or `discord.ClientException` / `app_commands.AppCommandError`, which happen when not logged in yet), return `ok=True` with message `"<success>; command tree sync failed: <e>"` and `synced=False`.
    - Other exceptions propagate (the dispatcher turns them into `internal_error`).
  - `load/reload/unload(name) -> str`: `return (await self.run(...)).message`.
  - `exit() -> None`: per 5.4.

### 8.6 `utils/loggerManager/*`
- `logSink.py`:
  - `_LEVELS` is unchanged.
  - `_FORMAT = "[%(asctime)s] [%(levelname)-7s] %(message)s"`, `_DATEFMT = "%Y-%m-%d %H:%M:%S"`.
  - `def _make_record(level: str, message: str, name: str) -> logging.LogRecord`: same `exc_info` logic as now for `"EXCEPTION"`. Sets `record.levelname = level`.
  - `FileSink`: `Path(file_path).parent.mkdir(parents=True, exist_ok=True)`. `RotatingFileHandler(..., maxBytes=5*1024*1024, backupCount=5, encoding="utf-8", delay=True)`. `write` calls **`self._handler.handle(record)`** (which takes the lock). `close()` is idempotent.
  - `ConsoleSink`: `logging.StreamHandler(stream)` with the same formatter. `close()` flushes only.
- `logger.py`: `Logger._dispatch` is unchanged in behaviour. A sink exception is reported once per sink instance via `logging.getLogger(__name__).warning("log sink %r failed", sink, exc_info=True)` (tracked in a private `set[int]` of `id(sink)`).
- `loggerManager.py`: no import-time I/O. Private `_log_dir: Path` (default `project_root()/"logs"`), `_console: bool` (default `False`; the CLI turns it on), `threading.Lock` around `create_logger`/`remove_logger`/`shutdown`. `create_logger` attaches `FileSink(log_dir / f"{name}.log")` and, if `_console`, `ConsoleSink()`.

### 8.7 `euphoria/settings.py`
```python
@dataclass(frozen=True, slots=True)
class BotSettings:
    token: str = field(repr=False)
    extensions: tuple[str, ...] = ()
    shutdown_timeout: float = 30.0
    timezone: str = "Europe/Moscow"

@dataclass(frozen=True, slots=True)
class ControlSettings:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 48600
    token: str | None = field(default=None, repr=False)
    allow_remote: bool = False
    state_file: Path = ...          # project_root()/".run"/"control.json"
    max_connections: int = 8
    idle_timeout: float = 60.0
    command_timeout: float = 120.0

@dataclass(frozen=True, slots=True)
class LoggingSettings:
    dir: Path = ...                 # project_root()/"logs"
    level: str = "INFO"
    console: bool = True

@dataclass(frozen=True, slots=True)
class AppSettings:
    bot: BotSettings
    control: ControlSettings
    logging: LoggingSettings
    database: DatabaseSettings

@dataclass(frozen=True, slots=True)
class CliOverrides:
    log_dir: Path | None = None
    control_host: str | None = None
    control_port: int | None = None
    control_enabled: bool | None = None
    console_log: bool | None = None

def load_settings(raw: Mapping[str, Any], *, base_dir: Path,
                  overrides: CliOverrides = CliOverrides()) -> AppSettings
def is_loopback(host: str) -> bool
```
- `load_settings`: pure. Validates types and ranges and raises `ConfigError` with the key path (e.g. `control.PORT`).
  - `bot.TOKEN` is required and must be a non-empty str.
  - Non-loopback host without `allow_remote=True` raises `ConfigError`. `allow_remote=True` without an explicit `token` (at least 16 chars) also raises `ConfigError`.
  - `level` must be one of `DEBUG|INFO|WARNING|ERROR`.
- `is_loopback`: `"localhost"` counts as loopback. Otherwise `ipaddress.ip_address(host).is_loopback`. Non-IP hostnames return `False`.

### 8.8 `euphoria/lifecycle.py`
```python
class ShutdownController:
    def __init__(self) -> None
    @property
    def requested(self) -> bool
    @property
    def reason(self) -> str | None
    @property
    def forced(self) -> bool
    def request(self, reason: str) -> None
    async def wait(self) -> str
    def attach_task(self, task: asyncio.Task[Any]) -> None
    def on_signal(self, signame: str) -> None

def install_signal_handlers(loop: asyncio.AbstractEventLoop,
                            controller: ShutdownController) -> Callable[[], None]
```
- `request(reason)`: idempotent. The first reason wins and the internal `asyncio.Event` is set. Must be called on the loop thread.
- `wait()`: awaits the event and returns the reason.
- `attach_task(task)`: registers the main task that `on_signal` cancels when forced.
- `on_signal(signame)`:
  - 1st call: `request(f"signal {signame}")`.
  - 2nd call: set `forced = True`, log an error ("forced shutdown"), and cancel the attached task.
  - Later calls are ignored.
- `install_signal_handlers`:
  - Uses `signal.signal` uniformly on all platforms (same technique as `asyncio.Runner`) for `SIGINT`, `SIGTERM`, and `SIGBREAK` when `hasattr(signal, "SIGBREAK")` (Windows Ctrl+Break).
  - The handler only does `loop.call_soon_threadsafe(controller.on_signal, signal.Signals(signum).name)`.
  - Returns a `restore()` that reinstates the previous handlers. Safe to call twice.
  - Must be called from the main thread. If not, it logs a warning and installs nothing (returns a no-op restore).
- Documented Windows limitation: closing the console window (`CTRL_CLOSE_EVENT`) and `taskkill /F` are not graceful. The supported service-stop path on Windows is `python -m euphoria ctl exit`.

### 8.9 `euphoria/control/protocol.py`
```python
PROTOCOL_VERSION: Final = 1
MAX_LINE_BYTES: Final = 64 * 1024
MAX_ARGS: Final = 16
MAX_ARG_LEN: Final = 256
COMMAND_RE: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")

class ErrorCode(StrEnum):
    OK = "ok"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN_COMMAND = "unknown_command"
    BAD_ARGUMENTS = "bad_arguments"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    BUSY = "busy"
    SHUTTING_DOWN = "shutting_down"
    INTERNAL_ERROR = "internal_error"

@dataclass(frozen=True, slots=True)
class Request:
    token: str = field(repr=False)
    command: str
    args: tuple[str, ...] = ()
    id: str | int | None = None

@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    code: ErrorCode
    message: str
    data: dict[str, Any] | list[Any] | None = None
    id: str | int | None = None

class ProtocolError(Exception):
    def __init__(self, code: ErrorCode, message: str, request_id: str | int | None = None) -> None

def parse_request(line: bytes) -> Request          # raises ProtocolError(BAD_REQUEST)
def encode_request(req: Request) -> bytes
def parse_response(line: bytes) -> Response        # raises ProtocolError(BAD_REQUEST)
def encode_response(resp: Response) -> bytes
```
- **Framing:** one JSON object per line, UTF-8, terminated by `\n`. `encode_*` produce compact JSON (`ensure_ascii=False`, `separators=(",", ":")`) plus `b"\n"`. Encoded output never contains a raw newline (JSON escapes it).
- **Request object:** `{"v": 1, "token": str, "command": str, "args": [str, ...], "id": str|int|null}`.
  - `v` is optional, but if present it must equal 1.
  - Unknown keys are rejected.
  - `command` must match `COMMAND_RE` (lowercased by the client, not by the server).
  - `args` holds at most `MAX_ARGS` strings, each at most `MAX_ARG_LEN`.
  - `id` is echoed back. It is preserved in `ProtocolError.request_id` when it could be parsed.
- **Response object:** `{"v": 1, "id": ..., "ok": bool, "code": str, "message": str, "data": ...|null}`. `ok` is true if and only if `code == "ok"`.
- Invalid UTF-8, invalid JSON, a non-object, or wrong field types raise `ProtocolError(BAD_REQUEST, ...)` with a short human message and no traceback.

### 8.10 `euphoria/control/commands.py`
The old `ICommand` hierarchy becomes a data-driven registry (no ABC).
```python
@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    message: str
    code: ErrorCode = ErrorCode.OK          # COMMAND_FAILED when ok is False
    data: dict[str, Any] | list[Any] | None = None

type CommandHandler = Callable[[tuple[str, ...]], Awaitable[CommandResult]]

@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    usage: str           # e.g. "load <extension>"
    summary: str
    min_args: int
    max_args: int
    handler: CommandHandler
    allowed_during_shutdown: bool = False

def build_default_commands(bot: Bot) -> list[CommandSpec]
```

| Command | Args | Handler behaviour | Old equivalent |
|---|---|---|---|
| `load` | 1 | `bot.module_manager.run("load", ext)`, mapped to `CommandResult(ok, message, code=OK or COMMAND_FAILED, data={"synced": ...})` | `LoadModuleCommand` |
| `reload` | 1 | same with `"reload"` | `ReloadModuleCommand` |
| `unload` | 1 | same with `"unload"` | `UnloadModuleCommand` |
| `exit` | 0 | `bot.request_shutdown("control: exit")`. Result `"Shutdown requested"`. `allowed_during_shutdown=True` | `ExitModuleCommand` |
| `list` | 0 | `data = sorted(bot.extensions)`. Message: `"<n> extension(s) loaded"` | new |
| `status` | 0 | `data = {"ready": bot.is_ready(), "user": str(bot.user) or None, "latency_ms": round(latency*1000) if finite else None, "guilds": len(bot.guilds), "extensions": sorted(...), "uptime_s": int, "shutting_down": bool}`. `allowed_during_shutdown=True` | new |
| `ping` | 0 | `"pong"`. `allowed_during_shutdown=True` | new |
| `help` | 0 | `data = [f"{usage} - {summary}" ...]` sorted by name. `allowed_during_shutdown=True` | new |

Extension names for `load/reload/unload` are validated with `re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", ext)`. On mismatch the handler returns `BAD_ARGUMENTS` without touching the bot. This blocks paths and relative imports.

### 8.11 `euphoria/control/dispatcher.py`
```python
class CommandDispatcher:
    def __init__(self, commands: Iterable[CommandSpec], *, timeout: float,
                 is_shutting_down: Callable[[], bool]) -> None
    def names(self) -> list[str]
    async def dispatch(self, name: str, args: Sequence[str]) -> CommandResult
```
- Duplicate command names in `__init__` raise `ValueError`.
- `dispatch` steps, in order:
  1. Unknown name: `UNKNOWN_COMMAND`, message `"unknown command '<name>'; try 'help'"`.
  2. `is_shutting_down()` and the command is not allowed during shutdown: `SHUTTING_DOWN`.
  3. Arity outside `[min, max]`: `BAD_ARGUMENTS`, message `"usage: <usage>"`.
  4. `async with asyncio.timeout(timeout)`: await the handler. On `TimeoutError`: `TIMEOUT`.
  5. Any other `Exception`: log `logger.exception(...)` with the full traceback and return `INTERNAL_ERROR` with message `"internal error, see logs"` (**never** a traceback on the wire).
  6. `asyncio.CancelledError` is re-raised.
- Every dispatch logs at INFO: command, args, resulting code, and duration.

### 8.12 `euphoria/control/server.py`
```python
class ControlServerError(Exception): ...

class ControlServer:
    def __init__(self, settings: ControlSettings, dispatcher: CommandDispatcher) -> None
    @property
    def address(self) -> tuple[str, int] | None
    @property
    def token(self) -> str
    @property
    def is_serving(self) -> bool
    async def start(self) -> None
    async def stop(self, grace: float = 2.0) -> None
```
- `token` is `settings.token` or `secrets.token_urlsafe(32)` generated in `__init__`.
- `start()`:
  1. Resolve with `socket.getaddrinfo(host, port, type=SOCK_STREAM)` and take the first result. Rev 2: resolution is inside the error handling; `socket.gaierror` (and any `OSError` from resolution) raises `ControlServerError("cannot resolve control host host:port: <reason>")` chained with `from exc`.
  2. Create the socket manually. On Windows, set `SO_EXCLUSIVEADDRUSE` (when `hasattr(socket, "SO_EXCLUSIVEADDRUSE")`) so another process cannot hijack the port. On POSIX, set `SO_REUSEADDR`.
  3. `bind`, `listen(16)`, `setblocking(False)`, then `asyncio.start_server(self._handle_client, sock=sock, limit=MAX_LINE_BYTES)`.
  4. Write the state file atomically.
  5. Log at INFO: `"control server listening on host:port"`. Log a WARNING if the host is not loopback.

  Errors: `OSError` on bind raises `ControlServerError("cannot bind control server to host:port (another instance running?)")`. Calling `start()` twice raises `RuntimeError`.
- `_handle_client(reader, writer)`: this per-connection task is tracked in `self._clients: set[asyncio.Task]`.
  - If `len(self._clients) > max_connections`: send `BUSY` and close.
  - Loop:
    1. `async with asyncio.timeout(idle_timeout): line = await reader.readline()`.
    2. EOF (`b""`): close.
    3. `ValueError` / `LimitOverrunError` (line too long): send `BAD_REQUEST "request too large"`, then close.
    4. `parse_request`. On `ProtocolError`: send it, then close.
    5. Token check with `hmac.compare_digest(req.token.encode(), self.token.encode())`. On failure: send `UNAUTHORIZED`, log a WARNING with the peer address (never the token), and close.
    6. `result = await dispatcher.dispatch(...)`, write the response, `await writer.drain()`.
  - Idle timeout: close silently.
  - `ConnectionError`: log at DEBUG and close.
  - Always `writer.close()` and `await writer.wait_closed()` with errors suppressed.
  - Requests on one connection are processed strictly sequentially.
- `stop(grace)`: idempotent.
  1. `server.close()` (stop accepting).
  2. `asyncio.wait(self._clients, timeout=grace)`.
  3. Remaining clients: `server.close_clients()` and cancel their tasks, then `asyncio.wait(..., timeout=1)`.
  4. `await server.wait_closed()` bounded by 1 s.
  5. Delete the state file if it exists and its JSON `pid == os.getpid()` (all errors suppressed and logged).

### 8.13 `euphoria/control/client.py`
```python
@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    token: str = field(repr=False)

class ClientError(Exception): ...

def resolve_endpoint(*, state_file: Path, host: str | None, port: int | None,
                     token: str | None, env: Mapping[str, str]) -> Endpoint
def send_command(endpoint: Endpoint, command: str, args: Sequence[str], *,
                 timeout: float) -> Response
def format_response(resp: Response) -> str
def run_ctl(ns: argparse.Namespace) -> int
```
- `resolve_endpoint` applies the precedence from 6.2. It raises `ClientError` if no token is found: `"no control token: bot not running or state file missing (use --token)"`.
- `send_command`:
  - Uses stdlib blocking `socket.create_connection((host, port), timeout=timeout)`, sends `encode_request`, and reads until `\n` (at most `MAX_LINE_BYTES`, or `ClientError`).
  - Returns `parse_response(...)`.
  - `ConnectionRefusedError` raises `ClientError("cannot connect to host:port: is the bot running?")`. `TimeoutError` raises `ClientError("timed out ...")`.
  - The `id` is a random short hex. A response `id` mismatch raises `ClientError`.
- `run_ctl`: wires these together, prints per 6.2, and returns 0 or 1.

### 8.14 `euphoria/bot.py`
```python
class Bot(commands.Bot):
    def __init__(self, settings: AppSettings | None = None, *,
                 database: Database | None = None,
                 shutdown: ShutdownController | None = None) -> None
    async def setup_hook(self) -> None
    def add_scheduler_task(self, func: Callable[..., Any], day: int | str, hour: int | str,
                           minute: int | str, id: str, args: tuple[Any, ...] = ()) -> None
    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None
    async def on_ready(self) -> None
    def request_shutdown(self, reason: str) -> None
    async def close(self) -> None
    # teardown steps (private, separately patchable in tests; each must not raise):
    async def _teardown(self) -> None
    async def _stop_control(self) -> None
    def _stop_scheduler(self) -> None
    async def _unload_extensions(self) -> None
    async def _close_gateway(self) -> None
    async def _close_database(self) -> None
```

**`__init__`**
- `super().__init__(command_prefix="/", help_command=None, intents=discord.Intents.all())`.
- `settings = settings or load_settings(cfg, base_dir=get_config_path().parent)`.
- Sets `self.logger`, `self.database = database or Database(settings=settings.database)`, `self.scheduler = AsyncIOScheduler(timezone=settings.bot.timezone)` (not started), `self.shutdown = shutdown or ShutdownController()`, and `self.module_manager = ModuleManager(self)`.
- Builds the dispatcher and `self.control_server = ControlServer(...)` if `settings.control.enabled`, else `None`.
- Also sets `self.started_at = time.monotonic()`, `self._synced = False`, `self._teardown_task: asyncio.Task[None] | None = None`, and `self.tree.on_error = self.on_app_command_error`.

**`setup_hook`** (runs after login; any exception aborts startup through `bot.start`)
1. `scheduler.start()`, then log "Scheduler started".
2. If `eager_connect`: `await database.get_pool()`, then log "Database connected successfully".
3. If there is a control server: `await control_server.start()`.
4. For each `ext` in `settings.bot.extensions`: `await module_manager.run("load", ext)`. A failure is logged and does not abort startup.

**`on_ready`**
- Presence is set as before.
- First time only: `tree.sync()` guarded by `discord.HTTPException` (logged), then log `"Bot {user} started successfully"`.

**`on_app_command_error`**
- Unwraps `CommandInvokeError.original` for logging.
- `CheckFailure` is logged at INFO. Everything else is logged with `self.logger.exception`.
- User texts are **identical** to the current ones.
- The `send_message`/`followup.send` call is wrapped in `try/except discord.HTTPException`, which is logged at WARNING.

**`request_shutdown(reason)`**: `self.shutdown.request(reason)`.

**`close()`**
- Calls `self.shutdown.request("close() called")` (a no-op if already requested).
- If `_teardown_task is None`, creates it with `asyncio.create_task(self._teardown(), name="euphoria-teardown")`.
- Then `await asyncio.shield(self._teardown_task)`.

**`_teardown()`**: the order is defined in section 9. Each step runs inside `try/except Exception` (logged) and its own timeout.

### 8.15 `euphoria/app.py`
```python
async def run_app(settings: AppSettings, *, bot_factory: Callable[[AppSettings, ShutdownController], Bot] = ...) -> int
```
1. `controller = ShutdownController()`, `controller.attach_task(asyncio.current_task())`, `restore = install_signal_handlers(asyncio.get_running_loop(), controller)`.
2. `bot = bot_factory(settings, controller)`. `start_task = create_task(bot.start(settings.bot.token))`, `stop_task = create_task(controller.wait())`.
3. `await asyncio.wait({start_task, stop_task}, return_when=FIRST_COMPLETED)`.
4. Exit code:
   - If `start_task` finished with an exception: log it (special messages for `discord.LoginFailure` "invalid token", `discord.PrivilegedIntentsRequired` "enable privileged intents in the developer portal", `ControlServerError`, and DB errors), exit code 1.
   - If `start_task` returned normally (closed from inside): exit code 0.
   - If shutdown was requested: exit code 0.
5. Teardown:
   - Rev 2: one overall budget. `deadline = loop.time() + settings.bot.shutdown_timeout`, computed once when teardown begins.
   - `async with asyncio.timeout_at(deadline): await bot.close()`. On `TimeoutError`: log an error, cancel `bot._teardown_task`, exit code 1.
   - Wait for `start_task` with `timeout = max(0.0, deadline - loop.time())` (no separate 5 s wait). Cancel `stop_task`.
   - Whatever is still pending (`start_task`, the cancelled `bot._teardown_task`, `stop_task`) is cancelled and awaited together with `asyncio.wait(..., timeout=CANCEL_GRACE)`, where module constant `CANCEL_GRACE: Final = 0.5`. Tasks still not done after that are abandoned (logged at WARNING); this never changes the exit code by itself.
   - Documented upper bound: the teardown phase of `run_app` (from shutdown trigger to return) takes at most `shutdown_timeout + CANCEL_GRACE` s, plus negligible scheduling overhead.
6. `except asyncio.CancelledError` (forced by a second signal): log and return 1. `finally: restore()`.
7. Return the exit code.

### 8.16 `euphoria/cli.py`
```python
def build_parser() -> argparse.ArgumentParser
def configure_logging(settings: LoggingSettings) -> None
def ensure_import_path(path: Path) -> None
def cmd_run(ns: argparse.Namespace) -> int
def main(argv: Sequence[str] | None = None) -> int
```
- `configure_logging`:
  - Root logger level from settings. `RotatingFileHandler(dir/"euphoria.log", 5 MB, 5, utf-8)` with format `"[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s"`. A `StreamHandler(sys.stderr)` if `console`.
  - `logging.getLogger("discord.http")` is set to WARNING.
  - Then `LoggerManager().configure(settings.dir, console=settings.console)`.
  - Idempotent: handlers it previously added are tracked and replaced, not duplicated.
- `ensure_import_path`: inserts `str(path)` at `sys.path[0]` if it is absent.
- `cmd_run`:
  1. `set_config_path` if `--config` was given. Load the raw config (`ConfigError` means print to stderr and return 1). `load_settings(...)`, then `configure_logging`, then `ensure_import_path(extensions_root or project_root()/"src")`.
  2. `rc = asyncio.run(run_app(settings))`.
  3. `finally`: `LoggerManager().shutdown()` and `logging.shutdown()`. Return `rc`.
- `main`:
  - With no subcommand, parse as `run`.
  - Dispatches to `cmd_run` or `client.run_ctl`.
  - Catches `KeyboardInterrupt` (possible only before handlers are installed) and returns 1.

### 8.17 `src/main.py` (shim)
```python
from euphoria.bot import Bot  # noqa: F401  (re-export for backward compatibility)
from euphoria.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["run"]))
```

## 9. Data flow

### 9.1 Startup
1. `python -m euphoria`: `cli.main`, then `cmd_run`.
2. Config path resolved, `cfg` loaded, `AppSettings` validated. Errors give exit code 1 with a one-line message.
3. Logging configured and the extension root added to `sys.path`.
4. `asyncio.run(run_app)`: signal handlers installed and `Bot` constructed (no I/O).
5. `bot.start(token)`: `login` (HTTP), then `setup_hook`:
   1. Scheduler starts.
   2. DB pool is created (eager).
   3. Control server binds and writes `.run/control.json`.
   4. Configured extensions are auto-loaded.
6. `connect`: gateway, then `on_ready`, then a one-time `tree.sync()`.

### 9.2 Control command
1. `ctl load modules.music`: `resolve_endpoint` reads `.run/control.json` (host, port, token).
2. TCP connect to 127.0.0.1:port and send `{"v":1,"token":"...","command":"load","args":["modules.music"],"id":"a1b2"}\n`.
3. On the server: `readline` (64 KiB limit), `parse_request`, constant-time token check, `dispatcher.dispatch`, `ModuleManager.run("load", ...)` under its lock, `load_extension`, `tree.sync()`, `CommandResult`.
4. The server sends `{"v":1,"id":"a1b2","ok":true,"code":"ok","message":"modules.music is loaded","data":{"synced":true}}\n`.
5. The client prints the message and exits 0.

### 9.3 Shutdown (all triggers converge on `Bot.close()`)
Triggers:
- Ctrl+C, SIGTERM or Ctrl+Break: `on_signal` then `request`.
- `ctl exit`: `request` (the response is written before the control server stops; see step 1 below).
- A cog calls `bot.close()`.
- `bot.start` fails.

`run_app` wakes up and runs `await bot.close()` under `SHUTDOWN_TIMEOUT`. `_teardown` then executes these steps **in order**. Each step catches and logs exceptions and is bounded by its own timeout, so one failing step never blocks the later ones.

| # | Step | Method | Timeout | Details |
|---|---|---|---|---|
| 1 | Stop accepting control commands | `_stop_control` | grace 2 s + 1 s abort | Listener closed. In-flight requests (e.g. the `exit` reply) finish, the rest are aborted. Dispatcher already answers `shutting_down` for mutating commands. State file removed. |
| 2 | Stop the scheduler | `_stop_scheduler` | n/a (sync) | `scheduler.shutdown(wait=False)` if `scheduler.running`. No new jobs fire into cogs that are being unloaded. |
| 3 | Unload cogs/extensions | `_unload_extensions` | 5 s per extension | For each extension in `reversed(list(self.extensions))`: `await self.unload_extension(name)`. Then `remove_cog` for any cog left. **No `tree.sync()`** during shutdown. Runs while the gateway is still open, so `cog_unload` hooks may still use Discord and the DB. |
| 4 | Close the Discord connection | `_close_gateway` | 10 s | `await super().close()`. discord.py's own extension loop is now a no-op. Closes voice, websocket and aiohttp session. Idempotent through discord's `_closing_task`. |
| 5 | Close the DB pool | `_close_database` | `DB_CLOSE_TIMEOUT` (inside `Database.close`) | After the gateway, so no new handler can use the DB. `Database` then refuses to re-create the pool. |
| 6 | Final log | (in `_teardown`) | n/a | `bot.logger.info("Bot stopped (reason: …)")` |
| 7 | Flush and close logs | `cli.cmd_run` `finally` | n/a | `LoggerManager().shutdown()` and `logging.shutdown()`, after the event loop has closed so that the last records are kept. |

After `asyncio.run` returns, there are no remaining tasks (asyncio.run cancels leftovers) and no non-daemon threads besides the main thread: the design uses no `to_thread` or executors, and `shutdown_default_executor` returns immediately.

## 10. Error handling strategy

| Situation | Handling |
|---|---|
| Config missing, malformed, or wrong types | `ConfigError` at startup. One-line message naming the key/path to stderr and the log. Exit code 1. |
| Login failure, privileged intents, DB unreachable, control port in use | Raised out of `bot.start`. `run_app` logs a specific message, still runs the full teardown (frees the aiohttp session, scheduler and partial pool), and exits with code 1. |
| Errors in an app command | Logged with a traceback. Ephemeral user message (unchanged texts). Send failures are logged and swallowed. |
| Extension load/reload/unload error | Logged with a traceback. A `str` result with `ok=False`. The process continues. |
| `tree.sync()` failure | Logged. The operation is still reported as successful with a sync warning. |
| Control protocol violation (bad JSON, too large, bad version, invalid UTF-8) | `bad_request` response, connection closed. Logged at WARNING with the peer address. |
| Wrong token | `unauthorized` response, connection closed. WARNING with the peer address. The token is never logged. |
| Handler exception | `internal_error` on the wire. Full traceback **only** in the logs. |
| Handler too slow | `timeout` after `COMMAND_TIMEOUT`. A partially loaded extension may remain (documented). |
| Too many connections | `busy`, connection closed. |
| DB query errors | Propagate to the calling cog unchanged (same as today). Writes are rolled back first. |
| Invalid SQL identifier | `ValueError` before any DB I/O. |
| Teardown step failure or timeout | Logged. The remaining steps still run. An overall timeout gives exit code 1. |
| Second signal during teardown | Forced: the main task is cancelled, exit code 1. Logs are still flushed by the `finally` in `cmd_run`. |
| A log sink failing | Swallowed (the logger must never break the bot), reported once to stdlib logging. |

**Rules:** no `print` outside `cli`/`client` user output, and no bare `except:`. `asyncio.CancelledError` is never swallowed, except at the top of `run_app` for the forced path.

## 11. Performance notes
- Scale: a single Discord bot, tens of guilds, a few control commands per day. No hot paths are introduced. The control server handles at most 8 concurrent connections. Request size is capped at 64 KiB, which bounds memory per connection.
- The control server costs no thread and no polling: it is event-driven on the existing loop. The old design kept a thread blocked permanently and polled every second.
- DB: the pool is unchanged. The lock in `get_pool` is taken only until the pool exists. After that the cost is one attribute check (double-checked).
- Logging: the file handler lock adds negligible cost. `delay=True` avoids opening files for unused loggers.
- `tree.sync()` is rate limited by Discord. It is called only after **successful** extension operations and once at startup.

## 12. Tooling: `pyproject.toml`
```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "bot-euphoria"
version = "0.2.0"
requires-python = ">=3.14"
dependencies = [
  "discord.py>=2.4",
  "aiomysql>=0.2",
  "PyMySQL>=1.1",
  "APScheduler>=3.10,<4",
]

[project.optional-dependencies]
dev = ["ruff>=0.8", "mypy>=1.13", "pytest>=8.3", "pytest-cov>=6.0"]

[project.scripts]
euphoria = "euphoria.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
include = ["euphoria*", "utils*"]
```
Tool configuration:
- `[tool.ruff]`, `[tool.ruff.lint]` and `[tool.mypy]` exactly as in STACK.
- Plus `[[tool.mypy.overrides]] module = ["aiomysql.*", "pymysql.*", "apscheduler.*"]` with `ignore_missing_imports = true`.
- `[tool.ruff.lint.per-file-ignores]`: `"src/utils/**" = ["N"]` (only needed if N is ever enabled; camelCase names are frozen).
- `[tool.pytest.ini_options]` per STACK plus `pythonpath = ["src"]`.
- `[tool.coverage.run] omit = ["src/main.py"]`.

`.gitignore` additions:
```
.venv/
.run/
!tests/fixtures/*.json
```
Keep `*.json` so `config.json` stays ignored.

## 13. Test guidance (for the developer and tester)
- **No network, no Discord token, no MySQL.**
- Async tests use `asyncio.run()` (no pytest-asyncio).
- The control server and client tests use a real loopback socket with `PORT=0` and `state_file` under `tmp_path`.
- The DB is tested by monkeypatching `aiomysql.create_pool` with an async fake returning a fake pool. The fake supports `acquire()` as an async context manager, a connection with `cursor()`, `commit()` and `rollback()`, and records executed SQL and params.
- The Bot is constructed with explicit `AppSettings` (control disabled or port 0), a fake `Database`, and a `ShutdownController`. Teardown order is checked by patching the five `_stop_*` / `_unload_*` / `_close_*` methods with recorders.
- `discord.Client.close` is patched with an `AsyncMock` where the real gateway would be touched.
- `conftest.py` provides:
  - an autouse fixture that resets the `LoggerManager` and `Config` singletons and points the log dir to `tmp_path`;
  - a `settings` factory fixture;
  - a `fake_pool` fixture.

## 14. Acceptance criteria

All criteria must be checkable through `.venv/Scripts/python -m pytest`, the quality gates, or simple inspection.

**Quality gates**
1. `python -m ruff check .` and `python -m ruff format --check .` are clean.
2. `python -m mypy src` is clean with `strict = true` (overrides only for aiomysql, pymysql and apscheduler).
3. `python -m pytest --cov=src --cov-report=term-missing` is all green, with coverage at least 85%.
4. `pip install -e ".[dev]"` succeeds in a fresh `.venv`. `python -m euphoria --help` and `python -m euphoria ctl --help` exit with code 0.
5. No source file starts with a UTF-8 BOM. `src/utils/inputManager/` and `src/__init__.py` no longer exist. `grep -rn "print(" src` matches only `euphoria/cli.py` and `euphoria/control/client.py`. There are no references to `/var/log`, `/tmp`, `os.mkfifo` or `os.path`.

**Frozen API** (`tests/test_frozen_api.py`, via `inspect.signature` and `hasattr`)

6. Every symbol in sections 5.1 to 5.6 exists at the stated import path, with the stated parameter names, order, kinds and defaults (`params=()`, `base='activities'`, `args=()`, `info=None`). `from utils.loggerManager import *` succeeds and imports `ConsoleSink`.
7. `import utils.config` and `import utils.database` perform no file I/O (no `config.json` needed). The first access to `utils.config.cfg` loads the file at `get_config_path()`. `Config().update_config()` after the file changes updates the same `cfg` object in place. (Rev 2) After loading, `set_config_path(other)` immediately makes a previously bound `cfg` name (`from utils.config import cfg`) contain the new file's content (same object, never `{}`); `set_config_path(missing)` raises `ConfigError` and leaves `cfg` and `get_config_path()` unchanged.
8. `import utils.loggerManager` creates no directories. Loggers created after `LoggerManager().configure(tmp_path)` write `tmp_path/<name>.log`. `LoggerManager().shutdown()` closes all handlers, so the files can be deleted on Windows right after.

**Database** (fake pool)

9. Twenty concurrent `get_pool()` calls on a fresh `Database` call `aiomysql.create_pool` exactly once.
10. `add`, `set`, `execute` and `delete` each call `commit()` once. On a cursor error they call `rollback()` and re-raise the original exception.
11. `add(1, {"a; DROP": 1})`, `get(1, base="x y")` and `set(1, {"`": 2})` raise `ValueError` without acquiring a connection. Valid names produce back-quoted SQL, e.g. ``INSERT INTO `activities` (`id`, `name`) VALUES (%s, %s)``. `set(1, {})` raises `ValueError`.
12. `DatabaseSettings.from_mapping` accepts both `"true"` and `true` for `DB_AUTOCOMMIT` and `DB_ECHO`. It raises `ConfigError` naming the key for a missing `DB_HOST` and for `DB_AUTOCOMMIT: "yes"`. Its `repr` does not contain the password.
13. `close()` is idempotent (a second call is a no-op). After `close()`, `get_pool()` raises `RuntimeError`. If `wait_closed()` hangs, `close()` returns within `close_timeout + 1.5` s and has called `terminate()`.

**Permissions**

14. The `isRole` predicate returns `False` (no exception) when `guild is None`, when `get_role` returns `None`, and when the user is not a `discord.Member`. It returns `True` or `False` correctly from `top_role` comparisons. `isUser`, `isOwner` and `isOwnerApp` predicates return the expected bools with mocked `is_owner`.

**ModuleManager**

15. `load`, `reload` and `unload` return `str` on both success and failure. Failure messages start with `"failed to <action> <name>:"`. `tree.sync` is called only after success. A `discord.HTTPException` from `sync` yields `ok=True, synced=False` and does not raise. Two concurrent `run()` calls are serialised (their recorded start/end intervals do not overlap).
16. `exit()` calls `bot.request_shutdown` and returns without awaiting `bot.close()`.

**Protocol & dispatcher**

17. `parse_request` rejects all of the following with `ProtocolError(BAD_REQUEST)`: invalid UTF-8, invalid JSON, a JSON array, a missing token, a missing command, a non-str arg, more than 16 args, an arg over 256 chars, `v: 2`, an unknown key, and a command not matching `COMMAND_RE`. `encode_response` output ends with exactly one `\n` and contains no other newline even when the message has `\n`. A request/response survives an encode/parse round trip unchanged, Cyrillic text included.
18. The dispatcher returns, respectively:
    - `UNKNOWN_COMMAND` for an unknown name;
    - `BAD_ARGUMENTS` for wrong arity (the message contains `usage:`);
    - `TIMEOUT` for a handler slower than `timeout`;
    - `INTERNAL_ERROR` with message `"internal error, see logs"` and no traceback text for a raising handler (the traceback is logged);
    - `SHUTTING_DOWN` for `load` while shutting down, while `ping`, `status` and `exit` are still allowed.
19. `load ../evil` and `load a/b` give `BAD_ARGUMENTS`, and `ModuleManager.run` is not called.

**Control server & client** (real loopback socket, port 0)

20. After `start()`, the state file exists with the correct host, port, token and pid. `ctl ping` through `send_command` returns `ok=True, message="pong"`. After `stop()`, the state file is gone and new connections are refused.
21. A wrong token returns `UNAUTHORIZED` and the server closes the connection. The token never appears in the log output (checked with `caplog`).
22. A request line over 64 KiB returns `BAD_REQUEST` and the connection is closed. The server stays alive and serves the next client.
23. Two requests sent on one connection get two responses in order with matching `id`s.
24. Opening `max_connections + 1` idle connections: the extra one receives `BUSY`. An idle connection is closed by the server after `idle_timeout` (set to 0.2 s in the test).
25. `stop(grace=0.2)` with a connected client that is blocked in a slow command returns within 2 s, and no client tasks remain.
26. Binding a second server to the same in-use port raises `ControlServerError`. (Rev 2) `start()` with a host for which `getaddrinfo` raises `socket.gaierror` (patched) raises `ControlServerError` whose `__cause__` is the `gaierror`, and no state file is written.
27. `load_settings` with `control.HOST = "0.0.0.0"` and no `ALLOW_REMOTE` raises `ConfigError`. With `ALLOW_REMOTE: true` and no token it raises `ConfigError`. `is_loopback` holds for `127.0.0.1`, `::1` and `localhost`, and not for `0.0.0.0` or `192.168.1.5`.
28. `run_ctl` returns 1 and prints `cannot connect` to stderr when nothing listens. It returns 1 with `no control token` when there is no state file and no token. It returns 1 when the server replies `ok=false`, and 0 for `ok=true`.

**Shutdown**

29. `ShutdownController`:
    - `request()` twice keeps the first reason, and `wait()` returns it.
    - `on_signal` once requests shutdown; the second call sets `forced` and cancels the attached task.
    - `install_signal_handlers` returns a `restore` that puts the previous `signal.getsignal` values back for SIGINT, SIGTERM and (on Windows) SIGBREAK.
30. `Bot.close()` called three times concurrently runs `_teardown` exactly once. The recorded step order is exactly `_stop_control, _stop_scheduler, _unload_extensions, _close_gateway, _close_database`.
31. If any single step raises or hangs past its timeout, the later steps still run and `close()` returns.
32. `_unload_extensions` unloads in reverse load order and does not call `tree.sync`. `_close_database` runs after `_close_gateway`. After teardown the scheduler is not running.
33. `Bot.close()` on a bot whose `setup_hook` never ran (no scheduler started, no pool, no control server started) completes without exceptions.
34. `run_app` with a patched `Bot.start` that waits forever:
    - `controller.request("test")` makes `run_app` return 0 and `bot.close` was awaited.
    - `Bot.start` raising `discord.LoginFailure` makes `run_app` return 1 with teardown still executed.
    - A teardown hanging longer than `shutdown_timeout` (0.3 s in the test) makes `run_app` return 1 in under 1.0 s (Rev 2: hard bound `shutdown_timeout + CANCEL_GRACE` = 0.8 s, plus slack), even though `Bot.start` never returns.
    - (Rev 2) With a fast teardown and a `Bot.start` that ignores `close()` and never returns, `run_app` returns 0 in under `shutdown_timeout + CANCEL_GRACE + 0.2` s and `start_task` is cancelled.
35. An end-to-end in-process test runs `run_app` with a fake bot start and a real control server on port 0. Sending `exit` through the client returns `ok=True` ("Shutdown requested"), then `run_app` returns 0, and afterwards `threading.enumerate()` contains only the main thread (plus pytest's own daemon threads, if any) and `asyncio.all_tasks()` from the loop is empty.
36. `cli.main(["run", "--config", <missing path>])` returns 1 with a message naming the path. `cli.main(["ctl"])` (no command) returns 2.

**Behavioural parity**

37. `on_app_command_error` sends exactly `"You don't have permission to use this command"` for `CheckFailure` and `"An error occurred while executing the command:\n{error}"` otherwise, through `followup.send` when `response.is_done()` and otherwise through `response.send_message`, both `ephemeral=True`. If sending raises `discord.NotFound`, the handler does not raise.
38. `add_scheduler_task` called **before** `setup_hook` registers the job (`scheduler.get_job(id)` is not `None`) with `CronTrigger(day_of_week=day, hour=hour, minute=minute)`, `replace_existing=True`, `misfire_grace_time=3600` and `coalesce=True`. An invalid trigger value (e.g. `hour=99`) is logged and does not raise.
39. The README documents: Windows/Linux support, `python -m euphoria`, `ctl` usage (load/reload/unload/list/status/ping/help/exit), the new config keys, the state file and token, shutdown behaviour, and the Windows limitation (closing the console window or `taskkill /F` is not graceful).

## 15. Revision log
| Date | Change | Why |
|---|---|---|
| 2026-10-22 | Initial design: audit, frozen API, socket control protocol, ordered teardown, tooling. | Refactor task from the orchestrator. |
| 2026-10-22 | Rev 2: 8.15 single `shutdown_timeout` deadline for `bot.close()` + `start_task`, then `CANCEL_GRACE` = 0.5 s; AC34(c) tightened, AC34(d) added. 8.12 `getaddrinfo` failures wrapped in `ControlServerError` (AC26). 5.1/8.2 `set_config_path` after load reloads eagerly in place (AC7). | TEST_REPORT NOTE-1 (run_app took shutdown_timeout + 5 s when `Bot.start` never returns), NOTE-4 (raw `gaierror`), stale-empty `cfg` hazard. |
