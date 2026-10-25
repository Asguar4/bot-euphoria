"""Synchronous client for the control channel (``python -m euphoria ctl``)."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from euphoria.control.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    Request,
    Response,
    encode_request,
    encode_response,
    parse_response,
)
from utils._paths import project_root

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48600


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where and how to reach the control server."""

    host: str
    port: int
    token: str = field(repr=False)


class ClientError(Exception):
    """Raised for connection, timeout and protocol problems."""


def _read_state(state_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_endpoint(
    *,
    state_file: Path,
    host: str | None,
    port: int | None,
    token: str | None,
    env: Mapping[str, str],
) -> Endpoint:
    """Resolve host/port/token from flags, env and the state file.

    Raises:
        ClientError: If no token can be found.
    """
    state = _read_state(state_file)
    resolved_token = token or env.get("EUPHORIA_CONTROL_TOKEN") or state.get("token")
    if not isinstance(resolved_token, str) or not resolved_token:
        raise ClientError("no control token: bot not running or state file missing (use --token)")
    resolved_host = host or state.get("host") or DEFAULT_HOST
    resolved_port = port if port is not None else state.get("port", DEFAULT_PORT)
    try:
        port_number = int(resolved_port)
    except TypeError, ValueError:
        raise ClientError(f"invalid control port: {resolved_port!r}") from None
    return Endpoint(str(resolved_host), port_number, resolved_token)


def send_command(
    endpoint: Endpoint, command: str, args: Sequence[str], *, timeout: float
) -> Response:
    """Send one command and return the parsed response.

    Raises:
        ClientError: On connection failure, timeout or protocol errors.
    """
    request_id = secrets.token_hex(4)
    payload = encode_request(Request(endpoint.token, command, tuple(args), request_id))
    where = f"{endpoint.host}:{endpoint.port}"
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=timeout) as sock:
            sock.sendall(payload)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_LINE_BYTES:
                    raise ClientError("response too large")
    except ConnectionRefusedError:
        raise ClientError(f"cannot connect to {where}: is the bot running?") from None
    except TimeoutError:
        raise ClientError(f"timed out talking to {where}") from None
    except OSError as e:
        raise ClientError(f"cannot connect to {where}: {e}") from e
    line = bytes(buf).split(b"\n", 1)[0]
    if not line:
        raise ClientError("connection closed without a response")
    try:
        resp = parse_response(line)
    except ProtocolError as e:
        raise ClientError(f"protocol error: {e.message}") from e
    if resp.id is not None and resp.id != request_id:
        raise ClientError("response id mismatch")
    return resp


def format_response(resp: Response) -> str:
    """Render a response for humans."""
    if not resp.ok:
        return f"error [{resp.code}]: {resp.message}"
    lines = [resp.message]
    if isinstance(resp.data, list):
        lines.extend(str(item) for item in resp.data)
    elif isinstance(resp.data, dict):
        lines.extend(f"{k}: {v}" for k, v in resp.data.items())
    return "\n".join(lines)


def run_ctl(ns: argparse.Namespace) -> int:
    """Run the ``ctl`` subcommand.

    Returns:
        0 on ``ok``, 1 otherwise.
    """
    state_file = Path(ns.state_file) if ns.state_file else project_root() / ".run" / "control.json"
    try:
        endpoint = resolve_endpoint(
            state_file=state_file,
            host=ns.host,
            port=ns.port,
            token=ns.token,
            env=os.environ,
        )
        resp = send_command(endpoint, str(ns.ctl_command).lower(), ns.ctl_args, timeout=ns.timeout)
    except ClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if ns.json:
        sys.stdout.write(encode_response(resp).decode("utf-8"))
    elif resp.ok:
        print(format_response(resp))
    else:
        print(format_response(resp), file=sys.stderr)
    return 0 if resp.ok else 1
