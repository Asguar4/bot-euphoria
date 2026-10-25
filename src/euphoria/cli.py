"""Command line interface: ``python -m euphoria [run|ctl]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from euphoria.control.client import run_ctl
from euphoria.settings import CliOverrides, LoggingSettings, load_settings
from utils._paths import project_root
from utils.config import Config, ConfigError, get_config_path, set_config_path
from utils.loggerManager import LoggerManager

_LOG_FORMAT = "[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s"
_installed_handlers: list[logging.Handler] = []


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="euphoria", description="Euphoria Discord bot")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the bot (default)")
    run.add_argument("--config", type=Path, help="path to config.json")
    run.add_argument("--log-dir", type=Path, help="log directory")
    run.add_argument("--control-host", help="control server host")
    run.add_argument("--control-port", type=int, help="control server port")
    run.add_argument("--no-control", action="store_true", help="disable the control server")
    run.add_argument("--no-console-log", action="store_true", help="disable console logging")
    run.add_argument("--extensions-root", type=Path, help="directory added to sys.path")

    ctl = sub.add_parser("ctl", help="send a command to a running bot")
    ctl.add_argument("--host")
    ctl.add_argument("--port", type=int)
    ctl.add_argument("--token")
    ctl.add_argument("--state-file", type=Path)
    ctl.add_argument("--timeout", type=float, default=130.0, help="seconds (default 130)")
    ctl.add_argument("--json", action="store_true", help="print the raw JSON response")
    ctl.add_argument("ctl_command", metavar="COMMAND")
    ctl.add_argument("ctl_args", metavar="ARG", nargs="*")
    return parser


def configure_logging(settings: LoggingSettings) -> None:
    """Configure stdlib logging and the LoggerManager (idempotent)."""
    root = logging.getLogger()
    for handler in _installed_handlers:
        root.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()

    root.setLevel(settings.level)
    formatter = logging.Formatter(_LOG_FORMAT)
    settings.dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.dir / "euphoria.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    handlers: list[logging.Handler] = [file_handler]
    if settings.console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
        _installed_handlers.append(handler)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    LoggerManager().configure(settings.dir, console=settings.console)


def ensure_import_path(path: Path) -> None:
    """Put ``path`` first on ``sys.path`` if absent."""
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def cmd_run(ns: argparse.Namespace) -> int:
    """Run the bot."""
    from euphoria.app import run_app

    try:
        if ns.config is not None:
            set_config_path(ns.config)
        raw = Config().config
        overrides = CliOverrides(
            log_dir=ns.log_dir,
            control_host=ns.control_host,
            control_port=ns.control_port,
            control_enabled=False if ns.no_control else None,
            console_log=False if ns.no_console_log else None,
        )
        settings = load_settings(raw, base_dir=get_config_path().parent, overrides=overrides)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    try:
        configure_logging(settings.logging)
        ensure_import_path(ns.extensions_root or project_root() / "src")
        return asyncio.run(run_app(settings))
    finally:
        LoggerManager().shutdown()
        logging.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        Exit code: 0 success, 1 runtime error, 2 usage error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("run", "ctl", "-h", "--help"):
        args.insert(0, "run")
    parser = build_parser()
    try:
        ns = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    try:
        if ns.command == "ctl":
            return run_ctl(ns)
        return cmd_run(ns)
    except KeyboardInterrupt:
        return 1
