"""Legacy entry point: ``cd src && python main.py``."""

from euphoria.bot import Bot  # noqa: F401
from euphoria.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["run"]))
