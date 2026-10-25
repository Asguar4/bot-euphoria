"""AC 9-13: Database behaviour against a fake pool."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from utils.config import ConfigError
from utils.database import Database, DatabaseSettings, quote_identifier


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_concurrent_get_pool_creates_once(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    """AC9."""

    async def go() -> list[Any]:
        db = Database(settings=db_settings)
        return await asyncio.gather(*(db.get_pool() for _ in range(20)))

    pools = run(go())
    assert fake_pool.created == 1
    assert all(p is fake_pool for p in pools)


def test_create_pool_kwargs(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    import aiomysql

    async def go() -> None:
        db = Database(settings=db_settings)
        await db.get_pool()

    run(go())
    kw = fake_pool.kwargs
    assert kw["host"] == "h" and kw["db"] == "d" and kw["cursorclass"] is aiomysql.DictCursor
    assert kw["conv"] is not None and kw["autocommit"] is True


def test_no_io_in_init() -> None:
    db = Database()
    assert db._pool is None and db._settings is None


def test_lazy_settings_from_cfg(tmp_path: Any, fake_pool: Any) -> None:
    import json

    from utils.config import set_config_path

    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "database": {
                    "DB_HOST": "hh",
                    "DB_PORT": 1,
                    "DB_USER": "u",
                    "DB_PASSWORD": "p",
                    "DB_NAME": "n",
                }
            }
        ),
        encoding="utf-8",
    )
    set_config_path(p)
    run(Database().get_pool())
    assert fake_pool.kwargs["host"] == "hh"


def test_converter_json() -> None:
    from pymysql.constants import FIELD_TYPE

    db = Database()
    assert db.converter[FIELD_TYPE.JSON]('{"a": "я"}') == {"a": "я"}


@pytest.mark.parametrize("method", ["execute", "add", "set", "delete"])
def test_writes_commit_once(db_settings: DatabaseSettings, fake_pool: Any, method: str) -> None:
    """AC10."""

    async def go() -> None:
        db = Database(settings=db_settings)
        if method == "execute":
            assert await db.execute("UPDATE t SET a=%s", (1,)) == 7
        elif method == "add":
            await db.add(1, {"name": "x"})
        elif method == "set":
            await db.set(1, {"name": "x"})
        else:
            await db.delete(1)

    run(go())
    c = fake_pool.conn
    assert c.commits == 1 and c.rollbacks == 0 and len(c.executed) == 1


@pytest.mark.parametrize("method", ["execute", "add", "set", "delete"])
def test_writes_rollback_and_reraise(
    db_settings: DatabaseSettings, fake_pool: Any, method: str
) -> None:
    """AC10."""
    err = RuntimeError("cursor boom")
    fake_pool.conn.fail_with = err

    async def go() -> None:
        db = Database(settings=db_settings)
        if method == "execute":
            await db.execute("x")
        elif method == "add":
            await db.add(1, {"name": "x"})
        elif method == "set":
            await db.set(1, {"name": "x"})
        else:
            await db.delete(1)

    with pytest.raises(RuntimeError) as ei:
        run(go())
    assert ei.value is err
    assert fake_pool.conn.rollbacks == 1 and fake_pool.conn.commits == 0


def test_rollback_failure_does_not_mask_original(
    db_settings: DatabaseSettings, fake_pool: Any
) -> None:
    fake_pool.conn.fail_with = ValueError("orig")
    fake_pool.conn.rollback_fails = True
    with pytest.raises(ValueError, match="orig"):
        run(Database(settings=db_settings).execute("x"))


def test_no_rollback_on_closed_connection(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    """OPT-5: aiomysql closes the connection on cancel; do not roll back."""
    fake_pool.conn.fail_with = ValueError("orig")
    fake_pool.conn.closed = True
    with pytest.raises(ValueError, match="orig"):
        run(Database(settings=db_settings).execute("x"))
    assert fake_pool.conn.rollbacks == 0


def test_valid_sql_is_quoted(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    """AC11."""

    async def go() -> None:
        db = Database(settings=db_settings)
        await db.add(1, {"name": "x", "ключ": 1} if False else {"name": "x"})
        await db.set(2, {"a": 1, "b": 2}, base="s.t")
        await db.delete(3, base="tbl")
        await db.get(4)

    run(go())
    q = [e[0] for e in fake_pool.conn.executed]
    assert q[0] == "INSERT INTO `activities` (`id`, `name`) VALUES (%s, %s)"
    assert q[1] == "UPDATE `s`.`t` SET `a` = %s, `b` = %s WHERE id = %s"
    assert q[2] == "DELETE FROM `tbl` WHERE id = %s"
    assert q[3] == "SELECT * FROM `activities` WHERE id = %s"
    assert fake_pool.conn.executed[0][1] == [1, "x"]
    assert fake_pool.conn.executed[1][1] == [1, 2, 2]


def test_add_default_info_none(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    run(Database(settings=db_settings).add(5))
    assert fake_pool.conn.executed[0][0] == "INSERT INTO `activities` (`id`) VALUES (%s)"


INJECTIONS = [
    "x y",
    "a; DROP TABLE t",
    "`",
    "a`b",
    "",
    " ",
    "1abc",
    "a.b.c",
    ".a",
    "a.",
    "a" * 65,
    "t--",
    "t/*",
    "a\nb",
    "tбл",
    "t\x00",
    "a`; DROP TABLE x; --",
    "x' OR '1'='1",
    "a-b",
]


@pytest.mark.parametrize("bad", INJECTIONS)
def test_quote_identifier_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        quote_identifier(bad)


@pytest.mark.parametrize("bad", [None, 5, b"abc", ["a"]])
def test_quote_identifier_non_str(bad: Any) -> None:
    with pytest.raises(ValueError):
        quote_identifier(bad)


@pytest.mark.parametrize(
    ("good", "out"),
    [("a", "`a`"), ("_x1", "`_x1`"), ("s.t", "`s`.`t`"), ("A" * 64, "`" + "A" * 64 + "`")],
)
def test_quote_identifier_ok(good: str, out: str) -> None:
    assert quote_identifier(good) == out


@pytest.mark.parametrize("bad", INJECTIONS)
def test_injection_via_base_and_columns_no_db_access(
    db_settings: DatabaseSettings, fake_pool: Any, bad: str
) -> None:
    """AC11: ValueError before any connection is acquired (or pool created)."""

    async def go() -> None:
        db = Database(settings=db_settings)
        for coro in (
            db.add(1, {bad: 1}),
            db.add(1, {"a": 1}, base=bad),
            db.set(1, {bad: 1}),
            db.set(1, {"a": 1}, base=bad),
            db.get(1, base=bad),
            db.delete(1, base=bad),
        ):
            with pytest.raises(ValueError):
                await coro

    run(go())
    assert fake_pool.acquires == 0
    assert fake_pool.created == 0


def test_set_empty_values_raises(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    with pytest.raises(ValueError):
        run(Database(settings=db_settings).set(1, {}))
    assert fake_pool.acquires == 0


def test_spec_examples(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    """AC11 literal examples."""

    async def go() -> None:
        db = Database(settings=db_settings)
        with pytest.raises(ValueError):
            await db.add(1, {"a; DROP": 1})
        with pytest.raises(ValueError):
            await db.get(1, base="x y")
        with pytest.raises(ValueError):
            await db.set(1, {"`": 2})

    run(go())
    assert fake_pool.acquires == 0


def test_params_are_bound_not_interpolated(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    evil = "'; DROP TABLE x; --"
    run(Database(settings=db_settings).add(1, {"name": evil}))
    sql, params = fake_pool.conn.executed[0]
    assert evil not in sql and evil in params


def test_fetchall_and_get(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    async def go() -> tuple[Any, Any]:
        db = Database(settings=db_settings)
        return await db.fetchall("SELECT 1"), await db.get(9)

    rows, one = run(go())
    assert rows == [{"id": 1}] and one == {"id": 1}


def test_close_idempotent_and_no_resurrection(
    db_settings: DatabaseSettings, fake_pool: Any
) -> None:
    """AC13."""

    async def go() -> None:
        db = Database(settings=db_settings)
        await db.get_pool()
        await db.close()
        assert fake_pool.closed
        await db.close()
        with pytest.raises(RuntimeError, match="Database is closed"):
            await db.get_pool()
        with pytest.raises(RuntimeError):
            await db.execute("x")
        with pytest.raises(RuntimeError):
            await db.fetchall("x")

    run(go())
    assert fake_pool.created == 1


def test_close_without_pool_then_get_pool_refused(
    db_settings: DatabaseSettings, fake_pool: Any
) -> None:
    async def go() -> None:
        db = Database(settings=db_settings)
        await db.close()
        with pytest.raises(RuntimeError):
            await db.get_pool()

    run(go())
    assert fake_pool.created == 0


def test_close_hanging_wait_closed_terminates(
    db_settings: DatabaseSettings, fake_pool: Any
) -> None:
    """AC13: returns within close_timeout + 1.5 s and terminate() called."""
    fake_pool.hang_wait_closed = True

    async def go() -> float:
        db = Database(settings=db_settings)
        await db.get_pool()
        t = time.perf_counter()
        await db.close()
        return time.perf_counter() - t

    elapsed = run(go())
    assert fake_pool.terminated
    assert elapsed < db_settings.close_timeout + 1.5


def test_close_never_raises(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    def boom() -> None:
        raise RuntimeError("close boom")

    fake_pool.close = boom

    async def go() -> None:
        db = Database(settings=db_settings)
        await db.get_pool()
        await db.close()

    run(go())


def test_close_while_get_pool_in_flight(db_settings: DatabaseSettings, fake_pool: Any) -> None:
    """Race: close() during pool creation must not leak a usable pool afterwards."""

    async def go() -> None:
        db = Database(settings=db_settings)
        t = asyncio.create_task(db.get_pool())
        await asyncio.sleep(0)
        await db.close()
        with pytest.raises(RuntimeError):
            await t
        with pytest.raises(RuntimeError):
            await db.get_pool()
        return None

    run(go())
    assert fake_pool.closed, "pool created concurrently with close() was leaked"


def test_get_pool_failure_propagates_and_retry(
    db_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiomysql

    calls = 0

    async def create_pool(**kw: Any) -> Any:
        nonlocal calls
        calls += 1
        raise OSError("refused")

    monkeypatch.setattr(aiomysql, "create_pool", create_pool)

    async def go() -> None:
        db = Database(settings=db_settings)
        for _ in range(2):
            with pytest.raises(OSError):
                await db.get_pool()

    run(go())
    assert calls == 2


def test_settings_error_names_key_when_cfg_incomplete(tmp_path: Any) -> None:
    import json

    from utils.config import set_config_path

    p = tmp_path / "c.json"
    p.write_text(json.dumps({"database": {"DB_PORT": 1}}), encoding="utf-8")
    set_config_path(p)
    with pytest.raises(ConfigError, match="DB_HOST"):
        run(Database().get_pool())
