"""Token-authenticated asyncio TCP control server."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from euphoria.control.dispatcher import CommandDispatcher
from euphoria.control.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    ProtocolError,
    Response,
    encode_response,
    parse_request,
)
from euphoria.settings import ControlSettings, is_loopback

logger = logging.getLogger(__name__)

FIRST_LINE_TIMEOUT = 5.0


class ControlServerError(Exception):
    """Raised when the control server cannot start."""


class ControlServer:
    """Newline-delimited JSON control server bound to the bot's event loop."""

    def __init__(self, settings: ControlSettings, dispatcher: CommandDispatcher) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._token = settings.token or secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self._address: tuple[str, int] | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._stopped = False

    @property
    def address(self) -> tuple[str, int] | None:
        """Bound ``(host, port)`` once started."""
        return self._address

    @property
    def token(self) -> str:
        """The authentication token."""
        return self._token

    @property
    def is_serving(self) -> bool:
        """Whether the server accepts connections."""
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        """Bind, listen and publish the state file.

        Raises:
            RuntimeError: If already started.
            ControlServerError: If the port cannot be bound.
        """
        if self._server is not None:
            raise RuntimeError("control server already started")
        host, port = self._settings.host, self._settings.port
        try:
            family, type_, proto, _, sockaddr = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )[0]
        except OSError as exc:
            raise ControlServerError(f"cannot resolve control host {host}:{port}: {exc}") from exc
        sock = socket.socket(family, type_, proto)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(16)
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            raise ControlServerError(
                f"cannot bind control server to {host}:{port} (another instance running?): {e}"
            ) from e
        self._server = await asyncio.start_server(
            self._handle_client, sock=sock, limit=MAX_LINE_BYTES
        )
        bound = sock.getsockname()
        self._address = (str(bound[0]), int(bound[1]))
        self._write_state_file()
        logger.info("control server listening on %s:%d", *self._address)
        if not is_loopback(host):
            logger.warning("control server is bound to non-loopback address %s", host)

    def _write_state_file(self) -> None:
        assert self._address is not None
        path = self._settings.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            path.parent.chmod(0o700)
        payload = {
            "host": self._address[0],
            "port": self._address[1],
            "token": self._token,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        if os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(path)

    def _remove_state_file(self) -> None:
        path: Path = self._settings.state_file
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("pid") == os.getpid():
                    path.unlink()
        except OSError, ValueError, AttributeError:
            logger.warning("could not remove state file %s", path, exc_info=True)

    async def _send(self, writer: asyncio.StreamWriter, resp: Response) -> None:
        writer.write(encode_response(resp))
        async with asyncio.timeout(self._settings.idle_timeout):
            await writer.drain()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        peer = writer.get_extra_info("peername")
        try:
            if len(self._clients) > self._settings.max_connections:
                await self._send(writer, Response(False, ErrorCode.BUSY, "too many connections"))
                return
            await self._serve(reader, writer, peer)
        except ConnectionError, TimeoutError:
            logger.debug("control connection %s closed abruptly", peer, exc_info=True)
        finally:
            self._clients.discard(task)
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except TimeoutError:
                writer.transport.abort()
            except Exception:
                logger.debug("control connection %s: error while closing", peer, exc_info=True)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: object
    ) -> None:
        authenticated = False
        while True:
            read_timeout = (
                self._settings.idle_timeout
                if authenticated
                else min(FIRST_LINE_TIMEOUT, self._settings.idle_timeout)
            )
            try:
                async with asyncio.timeout(read_timeout):
                    line = await reader.readline()
            except TimeoutError:
                return
            except ValueError:
                await self._send(
                    writer, Response(False, ErrorCode.BAD_REQUEST, "request too large")
                )
                return
            if not line:
                return
            try:
                req = parse_request(line)
            except ProtocolError as e:
                logger.warning("bad control request from %s: %s", peer, e.message)
                await self._send(writer, Response(False, e.code, e.message, id=e.request_id))
                return
            if not hmac.compare_digest(req.token.encode(), self._token.encode()):
                logger.warning("control request with wrong token from %s", peer)
                await self._send(
                    writer, Response(False, ErrorCode.UNAUTHORIZED, "unauthorized", id=req.id)
                )
                return
            authenticated = True
            result = await self._dispatcher.dispatch(req.command, req.args)
            await self._send(
                writer,
                Response(result.ok, result.code, result.message, result.data, req.id),
            )

    async def stop(self, grace: float = 2.0) -> None:
        """Stop serving (idempotent) and remove the state file."""
        if self._stopped:
            return
        self._stopped = True
        server = self._server
        if server is not None:
            server.close()
            self._remove_state_file()
            if self._clients:
                _, pending = await asyncio.wait(set(self._clients), timeout=grace)
                if pending:
                    server.abort_clients()
                    for t in pending:
                        t.cancel()
                    await asyncio.wait(pending, timeout=1)
            try:
                async with asyncio.timeout(1):
                    await server.wait_closed()
            except TimeoutError:
                logger.warning("control server wait_closed timed out")
