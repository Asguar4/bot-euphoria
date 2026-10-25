"""Async MySQL access layer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import aiomysql
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions

from utils.config import ConfigError

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _parse_bool(value: object, key: str) -> bool:
    """Parse a JSON bool or "true"/"false" string.

    Raises:
        ConfigError: For any other value.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ConfigError(f"database.{key}: expected a boolean or 'true'/'false', got {value!r}")


def _get(data: Mapping[str, Any], key: str, kind: type, default: Any = ...) -> Any:
    if key not in data:
        if default is ...:
            raise ConfigError(f"database.{key}: missing required key")
        return default
    value = data[key]
    ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        ok = True
    if not ok:
        raise ConfigError(f"database.{key}: expected {kind.__name__}, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Connection settings of the MySQL pool."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    db: str
    charset: str = "utf8mb4"
    autocommit: bool = True
    minsize: int = 1
    maxsize: int = 10
    pool_recycle: int = 3600
    connect_timeout: int = 10
    echo: bool = False
    close_timeout: float = 5.0
    eager_connect: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> DatabaseSettings:
        """Build settings from the ``database`` config section.

        Raises:
            ConfigError: On missing keys or wrong types.
        """
        minsize = int(_get(data, "DB_MINSIZE", int, 1))
        maxsize = int(_get(data, "DB_MAXSIZE", int, 10))
        if minsize > maxsize:
            raise ConfigError("database.DB_MINSIZE must be <= DB_MAXSIZE")
        return cls(
            host=_get(data, "DB_HOST", str),
            port=_get(data, "DB_PORT", int),
            user=_get(data, "DB_USER", str),
            password=_get(data, "DB_PASSWORD", str),
            db=_get(data, "DB_NAME", str),
            charset=_get(data, "DB_CHARSET", str, "utf8mb4"),
            autocommit=_parse_bool(data.get("DB_AUTOCOMMIT", True), "DB_AUTOCOMMIT"),
            minsize=minsize,
            maxsize=maxsize,
            pool_recycle=_get(data, "DB_POOL_RECYCLE", int, 3600),
            connect_timeout=_get(data, "DB_CONNECT_TIMEOUT", int, 10),
            echo=_parse_bool(data.get("DB_ECHO", False), "DB_ECHO"),
            close_timeout=float(_get(data, "DB_CLOSE_TIMEOUT", float, 5.0)),
            eager_connect=_parse_bool(data.get("DB_EAGER_CONNECT", True), "DB_EAGER_CONNECT"),
        )


def quote_identifier(name: str) -> str:
    """Validate and back-quote a SQL identifier (``ident`` or ``schema.ident``).

    Raises:
        ValueError: If the name is not a valid identifier.
    """
    parts = name.split(".") if isinstance(name, str) else []
    if not 1 <= len(parts) <= 2 or not all(_IDENTIFIER_RE.fullmatch(p) for p in parts):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return ".".join(f"`{p}`" for p in parts)


class Database:
    """Lazy, race-free aiomysql pool wrapper."""

    def __init__(self, *, settings: DatabaseSettings | None = None) -> None:
        self._settings = settings
        self._pool: aiomysql.Pool | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.converter: dict[Any, Any] = conversions.copy()
        self.converter[FIELD_TYPE.JSON] = json.loads

    def _get_settings(self) -> DatabaseSettings:
        if self._settings is None:
            from utils.config import cfg

            self._settings = DatabaseSettings.from_mapping(cfg["database"])
        return self._settings

    async def get_pool(self) -> aiomysql.Pool:
        """Return the pool, creating it once.

        Raises:
            RuntimeError: After ``close()``.
        """
        if self._closed:
            raise RuntimeError("Database is closed")
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._closed:
                raise RuntimeError("Database is closed")
            if self._pool is None:
                s = self._get_settings()
                try:
                    self._pool = await aiomysql.create_pool(
                        host=s.host,
                        port=s.port,
                        user=s.user,
                        password=s.password,
                        db=s.db,
                        charset=s.charset,
                        autocommit=s.autocommit,
                        cursorclass=aiomysql.DictCursor,
                        minsize=s.minsize,
                        maxsize=s.maxsize,
                        pool_recycle=s.pool_recycle,
                        connect_timeout=s.connect_timeout,
                        echo=s.echo,
                        conv=self.converter,
                    )
                except Exception:
                    logger.exception("database connection failed")
                    raise
                if self._closed:
                    late, self._pool = self._pool, None
                    late.close()
                    try:
                        async with asyncio.timeout(self._get_settings().close_timeout):
                            await late.wait_closed()
                    except Exception:
                        logger.exception("closing late database pool failed")
                    raise RuntimeError("Database is closed")
            return self._pool

    async def close(self) -> None:
        """Close the pool (idempotent, bounded, never raises)."""
        self._closed = True
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            timeout = self._get_settings().close_timeout
        except Exception:
            timeout = 5.0
        try:
            pool.close()
            try:
                async with asyncio.timeout(timeout):
                    await pool.wait_closed()
            except TimeoutError:
                logger.warning("database pool did not close in %.1fs; terminating", timeout)
                pool.terminate()
                async with asyncio.timeout(1):
                    await pool.wait_closed()
            logger.info("database connection closed")
        except Exception:
            logger.exception("error while closing database pool")

    async def _write(self, query: str, params: Sequence[Any]) -> int:
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    last: int = cur.lastrowid
                await conn.commit()
            except BaseException:
                if not conn.closed:
                    try:
                        await conn.rollback()
                    except Exception:
                        logger.warning("rollback failed", exc_info=True)
                raise
            return last

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        """Run a write statement and commit.

        Returns:
            ``cursor.lastrowid``.
        """
        return await self._write(query, params)

    async def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a query and return all rows as dicts."""
        pool = await self.get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(query, params)
            rows: list[dict[str, Any]] = await cur.fetchall()
            return rows

    async def add(
        self, id: int, info: Mapping[str, Any] | None = None, base: str = "activities"
    ) -> None:
        """Insert a row.

        Raises:
            ValueError: On an invalid table or column name.
        """
        info = info or {}
        table = quote_identifier(base)
        columns = [quote_identifier(c) for c in ["id", *info.keys()]]
        values = [id, *info.values()]
        placeholders = ", ".join(["%s"] * len(values))
        await self._write(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values
        )

    async def get(self, id: int, base: str = "activities") -> dict[str, Any] | None:
        """Fetch one row by id."""
        table = quote_identifier(base)
        pool = await self.get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(f"SELECT * FROM {table} WHERE id = %s", (id,))
            row: dict[str, Any] | None = await cur.fetchone()
            return row

    async def set(self, id: int, values: Mapping[str, Any], base: str = "activities") -> None:
        """Update columns of a row.

        Raises:
            ValueError: On empty ``values`` or invalid identifiers.
        """
        if not values:
            raise ValueError("values must not be empty")
        table = quote_identifier(base)
        assignments = ", ".join(f"{quote_identifier(k)} = %s" for k in values)
        await self._write(f"UPDATE {table} SET {assignments} WHERE id = %s", [*values.values(), id])

    async def delete(self, id: int, base: str = "activities") -> None:
        """Delete a row by id."""
        table = quote_identifier(base)
        await self._write(f"DELETE FROM {table} WHERE id = %s", (id,))
