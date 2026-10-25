"""Shared fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from euphoria.settings import (
    AppSettings,
    BotSettings,
    ControlSettings,
    LoggingSettings,
)
from utils import config as config_module
from utils.database import DatabaseSettings
from utils.loggerManager import LoggerManager

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_singletons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset Config/LoggerManager singletons and redirect logs to tmp_path."""
    monkeypatch.delenv("EUPHORIA_CONFIG", raising=False)
    monkeypatch.delenv("EUPHORIA_CONTROL_TOKEN", raising=False)

    def reset() -> None:
        if LoggerManager._instance is not None and hasattr(LoggerManager._instance, "loggers"):
            LoggerManager._instance.shutdown()
        LoggerManager._instance = None
        LoggerManager._initialized = False
        config_module._explicit_path = None
        config_module._shared = None
        config_module.Config._instance = None

    reset()
    LoggerManager().configure(tmp_path / "logs", console=False)
    yield
    reset()


@pytest.fixture
def db_settings() -> DatabaseSettings:
    return DatabaseSettings(host="h", port=1, user="u", password="pw", db="d", close_timeout=0.2)


@pytest.fixture
def make_settings(tmp_path: Path, db_settings: DatabaseSettings) -> Any:
    def factory(**control: Any) -> AppSettings:
        ctl = {
            "enabled": True,
            "port": 0,
            "state_file": tmp_path / "run" / "control.json",
            "idle_timeout": 5.0,
            "command_timeout": 5.0,
        }
        ctl.update(control)
        return AppSettings(
            bot=BotSettings(token="t", shutdown_timeout=5.0),
            control=ControlSettings(**ctl),
            logging=LoggingSettings(dir=tmp_path / "logs", console=False),
            database=db_settings,
        )

    return factory


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.lastrowid = 7

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.conn.executed.append((query, params))
        if self.conn.fail_with is not None:
            raise self.conn.fail_with

    async def fetchall(self) -> list[dict[str, Any]]:
        return [{"id": 1}]

    async def fetchone(self) -> dict[str, Any] | None:
        return {"id": 1}


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_with: BaseException | None = None
        self.rollback_fails = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_fails:
            raise RuntimeError("rollback boom")


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.acquired = False

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn()
        self.closed = False
        self.terminated = False
        self.hang_wait_closed = False
        self.acquires = 0

    def acquire(self) -> _Acquire:
        self.acquires += 1
        return _Acquire(self.conn)

    def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True
        self.hang_wait_closed = False

    async def wait_closed(self) -> None:
        if self.hang_wait_closed:
            await asyncio.sleep(3600)


@pytest.fixture
def fake_pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    """Patch aiomysql.create_pool; ``pool.created`` counts calls."""
    import aiomysql

    pool = FakePool()
    pool.created = 0  # type: ignore[attr-defined]

    async def create_pool(**kwargs: Any) -> FakePool:
        pool.created += 1  # type: ignore[attr-defined]
        pool.kwargs = kwargs  # type: ignore[attr-defined]
        await asyncio.sleep(0.01)
        return pool

    monkeypatch.setattr(aiomysql, "create_pool", create_pool)
    return pool
