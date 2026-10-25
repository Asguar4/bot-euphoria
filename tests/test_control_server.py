"""AC 20-26: control server over a real loopback socket."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from euphoria.control.client import Endpoint, send_command
from euphoria.control.commands import CommandResult, CommandSpec
from euphoria.control.dispatcher import CommandDispatcher
from euphoria.control.protocol import MAX_LINE_BYTES, ErrorCode, Request, encode_request
from euphoria.control.server import ControlServer, ControlServerError
from euphoria.settings import ControlSettings


async def ping(_a: tuple[str, ...]) -> CommandResult:
    return CommandResult(True, "pong")


def make_server(tmp_path: Path, slow: float = 0, **kw: Any) -> ControlServer:
    async def sleeper(_a: tuple[str, ...]) -> CommandResult:
        await asyncio.sleep(slow)
        return CommandResult(True, "slept")

    async def echo(a: tuple[str, ...]) -> CommandResult:
        return CommandResult(True, " ".join(a), data={"args": list(a)})

    specs = [
        CommandSpec("ping", "ping", "p", 0, 0, ping),
        CommandSpec("sleep", "sleep", "s", 0, 0, sleeper),
        CommandSpec("echo", "echo", "e", 0, 16, echo),
    ]
    st = ControlSettings(
        port=kw.pop("port", 0),
        state_file=tmp_path / "run" / "control.json",
        **kw,
    )
    return ControlServer(st, CommandDispatcher(specs, timeout=10, is_shutting_down=lambda: False))


async def rpc(server: ControlServer, raw: bytes, read_all: bool = False) -> list[bytes]:
    """Send raw bytes, read response lines until EOF (or one line)."""
    host, port = server.address  # type: ignore[misc]
    r, w = await asyncio.open_connection(host, port)
    w.write(raw)
    await w.drain()
    out: list[bytes] = []
    while True:
        try:
            line = await asyncio.wait_for(r.readline(), 3)
        except ValueError:
            break
        if not line:
            break
        out.append(line)
        if not read_all:
            break
    w.close()
    return out


def req(
    server: ControlServer, cmd: str = "ping", args: tuple[str, ...] = (), id: Any = "i1"
) -> bytes:
    return encode_request(Request(server.token, cmd, args, id))


def test_stop_within_step_budget_with_stuck_client(tmp_path: Path) -> None:
    """OPT-1: a peer that never reads must not push stop() past its budget."""

    async def big(_a: tuple[str, ...]) -> CommandResult:
        return CommandResult(True, "ok", data=["x" * 200] * 200)

    async def go() -> tuple[float, bool, list[asyncio.Task[Any]]]:
        srv = make_server(tmp_path, idle_timeout=30.0)
        srv._dispatcher._commands["big"] = CommandSpec("big", "big", "b", 0, 0, big)  # type: ignore[attr-defined]
        await srv.start()
        state = srv._settings.state_file
        _, w = await asyncio.open_connection(*srv.address)  # type: ignore[misc]
        for _ in range(400):
            w.write(req(srv, "big"))
        await w.drain()
        await asyncio.sleep(0.3)
        t = asyncio.get_running_loop().time()
        async with asyncio.timeout(3.0):
            await srv.stop(grace=0.3)
        took = asyncio.get_running_loop().time() - t
        await asyncio.sleep(0.1)
        left = [x for x in asyncio.all_tasks() if "_handle_client" in repr(x.get_coro())]
        w.close()
        return took, state.exists(), left

    took, state_exists, left = asyncio.run(go())
    assert took < 2.5
    assert not state_exists
    assert not left


def test_state_file_and_ping_and_stop(tmp_path: Path) -> None:
    """AC20."""

    async def go() -> None:
        s = make_server(tmp_path)
        assert not s.is_serving and s.address is None
        await s.start()
        assert s.is_serving
        host, port = s.address  # type: ignore[misc]
        assert host == "127.0.0.1" and port > 0
        state = json.loads((tmp_path / "run" / "control.json").read_text("utf-8"))
        assert state["host"] == host and state["port"] == port
        assert state["token"] == s.token and state["pid"] == os.getpid()
        assert "started_at" in state
        assert not list((tmp_path / "run").glob("*.tmp"))
        resp = await asyncio.to_thread(
            send_command, Endpoint(host, port, s.token), "ping", [], timeout=3
        )
        assert resp.ok and resp.message == "pong" and resp.code == ErrorCode.OK
        await s.stop()
        assert not (tmp_path / "run" / "control.json").exists()
        assert not s.is_serving
        with pytest.raises(OSError):
            await asyncio.open_connection(host, port)
        await s.stop()

    asyncio.run(go())


def test_start_twice_raises(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        with pytest.raises(RuntimeError):
            await s.start()
        await s.stop()

    asyncio.run(go())


def test_state_file_not_removed_if_pid_differs(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        f = tmp_path / "run" / "control.json"
        f.write_text(json.dumps({"pid": os.getpid() + 12345}), encoding="utf-8")
        await s.stop()
        assert f.exists()

    asyncio.run(go())


def test_state_file_garbage_does_not_raise(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        (tmp_path / "run" / "control.json").write_text("garbage", encoding="utf-8")
        await s.stop()

    asyncio.run(go())


def test_configured_token_used(tmp_path: Path) -> None:
    s = make_server(tmp_path, token="fixed-token-1234567")
    assert s.token == "fixed-token-1234567"
    assert make_server(tmp_path).token != make_server(tmp_path).token


def test_wrong_token_unauthorized_and_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC21."""

    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        bad = encode_request(Request("WRONG-TOKEN-xyz", "ping", (), "q"))
        with caplog.at_level(logging.DEBUG):
            lines = await rpc(s, bad, read_all=True)
        assert len(lines) == 1
        d = json.loads(lines[0])
        assert d["code"] == "unauthorized" and d["ok"] is False and d["id"] == "q"
        await s.stop()
        assert any("wrong token" in r.getMessage() for r in caplog.records)
        for r in caplog.records:
            assert "WRONG-TOKEN-xyz" not in r.getMessage()
            assert s.token not in r.getMessage()

    asyncio.run(go())


def test_wrong_token_second_request_not_served(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        bad = encode_request(Request("nope", "ping"))
        lines = await rpc(s, bad + req(s), read_all=True)
        assert len(lines) == 1
        await s.stop()

    asyncio.run(go())


def test_oversize_line_bad_request_then_server_alive(tmp_path: Path) -> None:
    """AC22."""

    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        big = b'{"token":"' + b"x" * (MAX_LINE_BYTES + 10) + b'"}\n'
        lines = await rpc(s, big, read_all=True)
        assert json.loads(lines[0])["code"] == "bad_request"
        assert "too large" in json.loads(lines[0])["message"]
        lines = await rpc(s, req(s))
        assert json.loads(lines[0])["message"] == "pong"
        lines = await rpc(s, b"y" * (MAX_LINE_BYTES * 3), read_all=True)
        assert lines and json.loads(lines[0])["code"] == "bad_request"
        lines = await rpc(s, req(s))
        assert json.loads(lines[0])["ok"] is True
        await s.stop()

    asyncio.run(go())


def test_line_just_under_limit_is_parsed(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        pad = MAX_LINE_BYTES - 200
        raw = b'{"token":"' + b"x" * pad + b'","command":"ping"}\n'
        lines = await rpc(s, raw, read_all=True)
        assert json.loads(lines[0])["code"] == "unauthorized"
        await s.stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    "raw",
    [b"garbage\n", b"\xff\xfe\n", b"[]\n", b'{"token":"t"}\n', b'{"token":"t","command":"Ping"}\n'],
)
def test_bad_requests_get_bad_request_and_close(tmp_path: Path, raw: bytes) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        lines = await rpc(s, raw, read_all=True)
        assert len(lines) == 1 and json.loads(lines[0])["code"] == "bad_request"
        await s.stop()

    asyncio.run(go())


def test_unknown_command_and_bad_arity(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        r = json.loads((await rpc(s, req(s, "nosuch")))[0])
        assert r["code"] == "unknown_command"
        r = json.loads((await rpc(s, req(s, "ping", ("x",))))[0])
        assert r["code"] == "bad_arguments"
        await s.stop()

    asyncio.run(go())


def test_two_requests_one_connection_ordered(tmp_path: Path) -> None:
    """AC23."""

    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        raw = req(s, "echo", ("a",), id=1) + req(s, "ping", id="two") + req(s, "echo", ("b",), id=3)
        lines = await rpc(s, raw, read_all=False)
        host, port = s.address  # type: ignore[misc]
        r, w = await asyncio.open_connection(host, port)
        w.write(raw)
        await w.drain()
        got = [json.loads(await asyncio.wait_for(r.readline(), 3)) for _ in range(3)]
        assert [g["id"] for g in got] == [1, "two", 3]
        assert [g["message"] for g in got] == ["a", "pong", "b"]
        w.close()
        assert lines
        await s.stop()

    asyncio.run(go())


def test_cyrillic_args_roundtrip(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        r = json.loads((await rpc(s, req(s, "echo", ("привет", "мир"))))[0])
        assert r["message"] == "привет мир" and r["data"]["args"] == ["привет", "мир"]
        await s.stop()

    asyncio.run(go())


def test_busy_when_over_max_connections(tmp_path: Path) -> None:
    """AC24."""

    async def go() -> None:
        s = make_server(tmp_path, max_connections=2, idle_timeout=5)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        idle = [await asyncio.open_connection(host, port) for _ in range(2)]
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection(host, port)
        line = await asyncio.wait_for(r.readline(), 2)
        assert json.loads(line)["code"] == "busy"
        assert await asyncio.wait_for(r.readline(), 2) == b""
        w.close()
        idle[0][1].close()
        await asyncio.sleep(0.2)
        lines = await rpc(s, req(s))
        assert json.loads(lines[0])["ok"] is True
        for _, ww in idle:
            ww.close()
        await s.stop()

    asyncio.run(go())


def test_idle_timeout_closes_connection(tmp_path: Path) -> None:
    """AC24."""

    async def go() -> None:
        s = make_server(tmp_path, idle_timeout=0.2)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        r, w = await asyncio.open_connection(host, port)
        t = asyncio.get_running_loop().time()
        assert await asyncio.wait_for(r.readline(), 3) == b""
        assert asyncio.get_running_loop().time() - t < 2
        w.close()
        await s.stop()

    asyncio.run(go())


def test_stop_with_slow_command_returns_quickly(tmp_path: Path) -> None:
    """AC25."""

    async def go() -> None:
        s = make_server(tmp_path, slow=30)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        _r, w = await asyncio.open_connection(host, port)
        w.write(req(s, "sleep"))
        await w.drain()
        await asyncio.sleep(0.2)
        t = asyncio.get_running_loop().time()
        await s.stop(grace=0.2)
        assert asyncio.get_running_loop().time() - t < 2
        assert not s._clients
        assert not (tmp_path / "run" / "control.json").exists()
        w.close()
        others = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
        assert others == []

    asyncio.run(go())


def test_stop_waits_for_inflight_reply_within_grace(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path, slow=0.3)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        r, w = await asyncio.open_connection(host, port)
        w.write(req(s, "sleep"))
        await w.drain()
        await asyncio.sleep(0.1)
        stopper = asyncio.create_task(s.stop(grace=2))
        line = await asyncio.wait_for(r.readline(), 3)
        assert json.loads(line)["message"] == "slept"
        w.close()
        await stopper

    asyncio.run(go())


def test_second_server_same_port_raises(tmp_path: Path) -> None:
    """AC26."""

    async def go() -> None:
        a = make_server(tmp_path)
        await a.start()
        port = a.address[1]  # type: ignore[index]
        b = make_server(tmp_path / "other", port=port)
        with pytest.raises(ControlServerError, match="cannot bind"):
            await b.start()
        assert not b.is_serving
        await a.stop()
        await b.stop()

    asyncio.run(go())


def test_port_not_hijackable_while_bound_plain_socket(tmp_path: Path) -> None:
    async def go() -> None:
        a = make_server(tmp_path)
        await a.start()
        port = a.address[1]  # type: ignore[index]
        s2 = socket.socket()
        try:
            with pytest.raises(OSError):
                s2.bind(("127.0.0.1", port))
        finally:
            s2.close()
        await a.stop()

    asyncio.run(go())


def test_port_reusable_after_stop(tmp_path: Path) -> None:
    async def go() -> None:
        a = make_server(tmp_path)
        await a.start()
        port = a.address[1]  # type: ignore[index]
        await a.stop()
        b = make_server(tmp_path, port=port)
        await b.start()
        await b.stop()

    asyncio.run(go())


def test_client_disconnect_midrequest_server_survives(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path, slow=0.2)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        _r, w = await asyncio.open_connection(host, port)
        w.write(req(s, "sleep"))
        await w.drain()
        w.close()
        await asyncio.sleep(0.5)
        assert json.loads((await rpc(s, req(s)))[0])["ok"]
        assert not s._clients or len(s._clients) <= 1
        await s.stop()

    asyncio.run(go())


def test_partial_line_then_eof(tmp_path: Path) -> None:
    async def go() -> None:
        s = make_server(tmp_path)
        await s.start()
        host, port = s.address  # type: ignore[misc]
        r, w = await asyncio.open_connection(host, port)
        w.write(b'{"token": "x"')
        await w.drain()
        w.write_eof()
        line = await asyncio.wait_for(r.readline(), 3)
        assert line == b"" or json.loads(line)["code"] == "bad_request"
        w.close()
        assert json.loads((await rpc(s, req(s)))[0])["ok"]
        await s.stop()

    asyncio.run(go())


def test_non_loopback_warning_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Server warns about non-loopback binding (uses 0.0.0.0, still local)."""

    async def go() -> None:
        s = make_server(tmp_path, host="0.0.0.0", allow_remote=True, token="x" * 20)
        with caplog.at_level(logging.WARNING):
            await s.start()
        await s.stop()

    asyncio.run(go())
    assert any("non-loopback" in r.getMessage() for r in caplog.records)


def test_ac26_unresolvable_host_controlservererror_chained_no_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC26 (Rev 2): gaierror from getaddrinfo -> ControlServerError from it, no state file."""
    gai = socket.gaierror(socket.EAI_NONAME, "name not known")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise gai

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    async def go() -> ControlServer:
        srv = make_server(tmp_path, host="nonexistent.invalid")
        with pytest.raises(ControlServerError, match="cannot resolve") as ei:
            await srv.start()
        assert ei.value.__cause__ is gai
        assert not srv.is_serving
        await srv.stop()
        return srv

    asyncio.run(go())
    assert not (tmp_path / "run" / "control.json").exists()
