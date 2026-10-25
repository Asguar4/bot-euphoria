"""AC 28 + client details: endpoint resolution, exit codes, protocol errors."""

from __future__ import annotations

import argparse
import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from euphoria.control.client import (
    ClientError,
    Endpoint,
    format_response,
    resolve_endpoint,
    run_ctl,
    send_command,
)
from euphoria.control.protocol import ErrorCode, Response


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def fake_server(
    reply: Callable[[dict[str, Any]], bytes | None],
) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """One-shot server: reads a line, answers with reply(request) bytes (None: close)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    seen: list[dict[str, Any]] = []
    stop = threading.Event()

    def loop() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError, OSError:
                continue
            with conn:
                buf = b""
                conn.settimeout(2)
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    continue
                if not buf:
                    continue
                req = json.loads(buf)
                seen.append(req)
                out = reply(req)
                if out is not None:
                    conn.sendall(out)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1], seen
    finally:
        stop.set()
        t.join(2)
        srv.close()


def ok_reply(req: dict[str, Any], **over: Any) -> bytes:
    obj = {"v": 1, "id": req["id"], "ok": True, "code": "ok", "message": "pong", "data": None}
    obj.update(over)
    return json.dumps(obj, ensure_ascii=False).encode() + b"\n"


def ns(tmp_path: Path, port: int | None = None, **kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = dict(
        host="127.0.0.1",
        port=port,
        token="tok",
        state_file=tmp_path / "none.json",
        timeout=3.0,
        json=False,
        ctl_command="ping",
        ctl_args=[],
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_precedence(tmp_path: Path) -> None:
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"host": "10.0.0.1", "port": 111, "token": "state"}), "utf-8")
    env = {"EUPHORIA_CONTROL_TOKEN": "env"}
    e = resolve_endpoint(state_file=sf, host=None, port=None, token=None, env={})
    assert (e.host, e.port, e.token) == ("10.0.0.1", 111, "state")
    e = resolve_endpoint(state_file=sf, host=None, port=None, token=None, env=env)
    assert e.token == "env"
    e = resolve_endpoint(state_file=sf, host="h", port=5, token="flag", env=env)
    assert (e.host, e.port, e.token) == ("h", 5, "flag")


def test_resolve_defaults_with_token_only(tmp_path: Path) -> None:
    e = resolve_endpoint(state_file=tmp_path / "x", host=None, port=None, token="t", env={})
    assert (e.host, e.port) == ("127.0.0.1", 48600)


def test_resolve_port_zero_flag_wins(tmp_path: Path) -> None:
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"port": 9, "token": "t"}), "utf-8")
    assert resolve_endpoint(state_file=sf, host=None, port=0, token=None, env={}).port == 0


@pytest.mark.parametrize("content", [None, "garbage", "[]", "{}", '{"token": ""}', '{"token": 5}'])
def test_resolve_no_token(tmp_path: Path, content: str | None) -> None:
    sf = tmp_path / "s.json"
    if content is not None:
        sf.write_text(content, "utf-8")
    with pytest.raises(ClientError, match="no control token"):
        resolve_endpoint(state_file=sf, host=None, port=None, token=None, env={})


def test_endpoint_repr_hides_token() -> None:
    assert "secret" not in repr(Endpoint("h", 1, "secret"))


def test_resolve_corrupt_port_in_state_file(tmp_path: Path) -> None:
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"port": "abc", "token": "t"}), "utf-8")
    try:
        resolve_endpoint(state_file=sf, host=None, port=None, token=None, env={})
    except ClientError:
        pass
    else:
        pytest.fail("expected ClientError")


def test_run_ctl_connection_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_ctl(ns(tmp_path, port=free_port())) == 1
    err = capsys.readouterr().err
    assert "cannot connect" in err


def test_run_ctl_no_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_ctl(ns(tmp_path, port=1, token=None)) == 1
    assert "no control token" in capsys.readouterr().err


def test_run_ctl_ok_true(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with fake_server(lambda r: ok_reply(r)) as (port, seen):
        assert run_ctl(ns(tmp_path, port=port)) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "pong" and out.err == ""
    assert seen[0]["command"] == "ping" and seen[0]["token"] == "tok" and seen[0]["v"] == 1


def test_run_ctl_ok_false(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def reply(r: dict[str, Any]) -> bytes:
        return ok_reply(r, ok=False, code="command_failed", message="bad thing")

    with fake_server(reply) as (port, _):
        assert run_ctl(ns(tmp_path, port=port)) == 1
    cap = capsys.readouterr()
    assert "error [" in cap.err and "bad thing" in cap.err and cap.out == ""


def test_run_ctl_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with fake_server(lambda r: ok_reply(r, message="привет")) as (port, _):
        assert run_ctl(ns(tmp_path, port=port, json=True)) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["message"] == "привет" and d["ok"] is True


def test_run_ctl_args_passed(tmp_path: Path) -> None:
    with fake_server(lambda r: ok_reply(r)) as (port, seen):
        run_ctl(ns(tmp_path, port=port, ctl_command="load", ctl_args=["modules.я"]))
    assert seen[0]["args"] == ["modules.я"]


def test_run_ctl_timeout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import time

    def slow(_r: dict[str, Any]) -> bytes | None:
        time.sleep(1.0)
        return None

    with fake_server(slow) as (port, _):
        assert run_ctl(ns(tmp_path, port=port, timeout=0.2)) == 1
    assert "timed out" in capsys.readouterr().err


def test_run_ctl_server_closes_without_response(tmp_path: Path) -> None:
    with fake_server(lambda r: None) as (port, _):
        assert run_ctl(ns(tmp_path, port=port)) == 1


def test_run_ctl_garbage_response(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with fake_server(lambda r: b"garbage\n") as (port, _):
        assert run_ctl(ns(tmp_path, port=port)) == 1
    assert "protocol error" in capsys.readouterr().err


def test_run_ctl_id_mismatch(tmp_path: Path) -> None:
    with fake_server(lambda r: ok_reply(r, id="different")) as (port, _):
        assert run_ctl(ns(tmp_path, port=port)) == 1


def test_run_ctl_response_too_large(tmp_path: Path) -> None:
    with fake_server(lambda r: b"x" * 200_000) as (port, _):
        assert run_ctl(ns(tmp_path, port=port)) == 1


def test_run_ctl_uses_state_file(tmp_path: Path) -> None:
    with fake_server(lambda r: ok_reply(r)) as (port, seen):
        sf = tmp_path / "state.json"
        sf.write_text(
            json.dumps({"host": "127.0.0.1", "port": port, "token": "fromstate"}), "utf-8"
        )
        assert run_ctl(ns(tmp_path, port=None, host=None, token=None, state_file=sf)) == 0
    assert seen[0]["token"] == "fromstate"


def test_send_command_unresolvable_host() -> None:
    with pytest.raises(ClientError):
        send_command(Endpoint("no-such-host.invalid", 1, "t"), "ping", [], timeout=2)


def test_ctl_uppercase_command_is_lowercased_by_client(tmp_path: Path) -> None:
    """DESIGN 8.9: command is lowercased by the client, not by the server."""
    with fake_server(lambda r: ok_reply(r)) as (port, seen):
        run_ctl(ns(tmp_path, port=port, ctl_command="PING"))
    assert seen[0]["command"] == "ping"


def test_format_response_variants() -> None:
    assert format_response(Response(True, ErrorCode.OK, "m")) == "m"
    assert format_response(Response(True, ErrorCode.OK, "m", ["a", "b"])) == "m\na\nb"
    assert format_response(Response(True, ErrorCode.OK, "m", {"k": 1, "j": "я"})) == "m\nk: 1\nj: я"
    assert format_response(Response(False, ErrorCode.BUSY, "x")) == "error [busy]: x"
