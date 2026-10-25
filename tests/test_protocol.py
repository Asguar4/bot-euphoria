"""AC 17: wire protocol."""

from __future__ import annotations

import json
from typing import Any

import pytest

from euphoria.control.protocol import (
    COMMAND_RE,
    MAX_ARG_LEN,
    MAX_ARGS,
    MAX_LINE_BYTES,
    ErrorCode,
    ProtocolError,
    Request,
    Response,
    encode_request,
    encode_response,
    parse_request,
    parse_response,
)


def line(**over: Any) -> bytes:
    obj: dict[str, Any] = {"v": 1, "token": "t", "command": "ping", "args": [], "id": "x"}
    obj.update(over)
    for k in [k for k, v in obj.items() if v is ...]:
        del obj[k]
    return json.dumps(obj).encode() + b"\n"


def test_constants() -> None:
    assert MAX_LINE_BYTES == 65536 and MAX_ARGS == 16 and MAX_ARG_LEN == 256


def test_valid_minimal() -> None:
    r = parse_request(b'{"token":"t","command":"ping"}\n')
    assert r == Request("t", "ping", (), None)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe{}\n",
        b"not json\n",
        b"\n",
        b"",
        b"[]\n",
        b'"s"\n',
        b"1\n",
        b"null\n",
        line(token=...),
        line(command=...),
        line(token=5),
        line(token=None),
        line(command=5),
        line(args=[1]),
        line(args="ping"),
        line(args=[None]),
        line(args=["a"] * 17),
        line(args=["x" * 257]),
        line(v=2),
        line(v="1"),
        line(v=True),
        line(extra=1),
        line(id=[1]),
        line(id=1.5),
        line(id=True),
        line(command="Ping"),
        line(command="1ping"),
        line(command=""),
        line(command="a b"),
        line(command="a/b"),
        line(command="x" * 33),
        line(command="ping\n"),
    ],
)
def test_parse_request_rejects(raw: bytes) -> None:
    with pytest.raises(ProtocolError) as ei:
        parse_request(raw)
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert "Traceback" not in ei.value.message


def test_boundaries_accepted() -> None:
    r = parse_request(line(args=["x" * 256] * 16, command="a" * 32))
    assert len(r.args) == 16 and len(r.args[0]) == 256
    assert parse_request(line(v=1, id=7)).id == 7
    assert parse_request(line(id=None)).id is None
    assert parse_request(line(v=...)).command == "ping"


def test_error_preserves_request_id() -> None:
    with pytest.raises(ProtocolError) as ei:
        parse_request(line(v=2, id="abc"))
    assert ei.value.request_id == "abc"


def test_command_re() -> None:
    assert COMMAND_RE.fullmatch("a-b_c1")
    assert not COMMAND_RE.fullmatch("A")


def test_request_roundtrip_cyrillic() -> None:
    req = Request("токен", "load", ("модуль", "a\nb", "😀"), "id-1")
    enc = encode_request(req)
    assert enc.endswith(b"\n") and enc.count(b"\n") == 1
    assert "модуль".encode() in enc
    assert parse_request(enc) == req


def test_request_repr_hides_token() -> None:
    assert "secret" not in repr(Request("secret", "ping"))


@pytest.mark.parametrize("code", list(ErrorCode))
def test_response_roundtrip(code: ErrorCode) -> None:
    resp = Response(code == ErrorCode.OK, code, "сообщение\nс переводом\r\n", {"k": [1, "я"]}, 5)
    enc = encode_response(resp)
    assert enc.endswith(b"\n") and enc.count(b"\n") == 1 and b"\r" not in enc
    assert parse_response(enc) == resp


def test_response_ok_iff_code_ok() -> None:
    enc = encode_response(Response(True, ErrorCode.BUSY, "m"))
    assert json.loads(enc)["ok"] is False
    enc = encode_response(Response(False, ErrorCode.OK, "m"))
    assert json.loads(enc)["ok"] is True


def test_response_data_list_and_none() -> None:
    assert parse_response(encode_response(Response(True, ErrorCode.OK, "m", ["a"]))).data == ["a"]
    assert parse_response(encode_response(Response(True, ErrorCode.OK, "m"))).data is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x\n",
        b"[]\n",
        b'{"ok":true}\n',
        b'{"ok":"yes","code":"ok","message":"m"}\n',
        b'{"ok":true,"code":"nope","message":"m"}\n',
        b'{"ok":true,"code":"ok","message":5}\n',
        b'{"ok":true,"code":"ok","message":"m","data":5}\n',
        b'{"ok":true,"code":"ok","message":"m","id":[1]}\n',
        b"\xff\n",
    ],
)
def test_parse_response_rejects(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_response(raw)
