"""Wire format of the control channel: newline-delimited JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
MAX_LINE_BYTES: Final = 64 * 1024
MAX_ARGS: Final = 16
MAX_ARG_LEN: Final = 256
COMMAND_RE: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class ErrorCode(StrEnum):
    """Response codes."""

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
    """A control request."""

    token: str = field(repr=False)
    command: str
    args: tuple[str, ...] = ()
    id: str | int | None = None


@dataclass(frozen=True, slots=True)
class Response:
    """A control response."""

    ok: bool
    code: ErrorCode
    message: str
    data: dict[str, Any] | list[Any] | None = None
    id: str | int | None = None


class ProtocolError(Exception):
    """Malformed message."""

    def __init__(self, code: ErrorCode, message: str, request_id: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def _loads(line: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(line.decode("utf-8"))
    except UnicodeDecodeError:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "invalid UTF-8") from None
    except json.JSONDecodeError:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "invalid JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "message must be a JSON object")
    return obj


def _dumps(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _check_id(value: Any) -> str | int | None:
    if (
        value is None
        or isinstance(value, str)
        or (isinstance(value, int) and not isinstance(value, bool))
    ):
        return value
    raise ProtocolError(ErrorCode.BAD_REQUEST, "id must be a string, integer or null")


def parse_request(line: bytes) -> Request:
    """Parse and validate one request line.

    Raises:
        ProtocolError: ``BAD_REQUEST`` on any violation.
    """
    obj = _loads(line)
    raw_id = obj.get("id")
    req_id: str | int | None = (
        raw_id
        if isinstance(raw_id, str) or (isinstance(raw_id, int) and not isinstance(raw_id, bool))
        else None
    )

    def bad(message: str) -> ProtocolError:
        return ProtocolError(ErrorCode.BAD_REQUEST, message, req_id)

    unknown = set(obj) - {"v", "token", "command", "args", "id"}
    if unknown:
        raise bad(f"unknown field(s): {', '.join(sorted(map(str, unknown)))}")
    if "v" in obj and (obj["v"] != PROTOCOL_VERSION or isinstance(obj["v"], bool)):
        raise bad("unsupported protocol version")
    token = obj.get("token")
    if not isinstance(token, str):
        raise bad("token must be a string")
    command = obj.get("command")
    if not isinstance(command, str) or not COMMAND_RE.fullmatch(command):
        raise bad("invalid command")
    args = obj.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise bad("args must be a list of strings")
    if len(args) > MAX_ARGS:
        raise bad(f"too many arguments (max {MAX_ARGS})")
    if any(len(a) > MAX_ARG_LEN for a in args):
        raise bad(f"argument too long (max {MAX_ARG_LEN})")
    try:
        _check_id(raw_id)
    except ProtocolError as e:
        raise bad(e.message) from None
    return Request(token=token, command=command, args=tuple(args), id=req_id)


def encode_request(req: Request) -> bytes:
    """Encode a request as one JSON line."""
    return _dumps(
        {
            "v": PROTOCOL_VERSION,
            "token": req.token,
            "command": req.command,
            "args": list(req.args),
            "id": req.id,
        }
    )


def parse_response(line: bytes) -> Response:
    """Parse one response line.

    Raises:
        ProtocolError: ``BAD_REQUEST`` on malformed responses.
    """
    obj = _loads(line)
    try:
        code = ErrorCode(obj["code"])
        message = obj["message"]
        ok = obj["ok"]
        data = obj.get("data")
        rid = _check_id(obj.get("id"))
    except KeyError, ValueError:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed response") from None
    if (
        not isinstance(message, str)
        or not isinstance(ok, bool)
        or not (data is None or isinstance(data, dict | list))
    ):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed response")
    return Response(ok=ok, code=code, message=message, data=data, id=rid)


def encode_response(resp: Response) -> bytes:
    """Encode a response as one JSON line."""
    return _dumps(
        {
            "v": PROTOCOL_VERSION,
            "id": resp.id,
            "ok": resp.code == ErrorCode.OK,
            "code": str(resp.code),
            "message": resp.message,
            "data": resp.data,
        }
    )
