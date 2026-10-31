"""The control endpoint's byte stream: a Unix socket on Linux and macOS, localhost TCP on Windows.

``path`` is what ``platform.socket_path()`` returns. A ``.json`` path means TCP: the server
listens on 127.0.0.1 (a free port) and writes ``{"port": ..., "token": ...}`` there; a client
reads it, connects and sends the token as its first line, and the server drops anyone who does
not. The token stands in for the Unix socket's 0600 mode: other users can reach a localhost
port but cannot read the file in the user's own profile. After the handshake both work the
same: one command line in, JSON lines out.

Stdlib only (``zundamon-jarvisctl`` is run by the status bar every few seconds).
"""

import asyncio
import hmac
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

HANDSHAKE_TIMEOUT_S = 5.0

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def is_tcp(path: Path) -> bool:
    return Path(path).suffix == ".json"


def _endpoint(path: Path) -> tuple[int, str]:
    """(port, token) from the endpoint file; ConnectionRefusedError if there is none usable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        port, token = int(data["port"]), str(data["token"])
    except FileNotFoundError:
        raise  # not running
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ConnectionRefusedError(f"unusable control endpoint {path}: {e}") from None
    return port, token


def connect(path: Path, timeout: float) -> socket.socket:
    """A stream socket connected to the daemon (past the handshake). Raises OSError if none
    is listening; ``timeout`` is left set on the socket."""
    path = Path(path)
    if not is_tcp(path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect(str(path))
        except BaseException:
            s.close()
            raise
        return s
    port, token = _endpoint(path)
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(token.encode() + b"\n")
    except BaseException:
        s.close()
        raise
    return s


def is_live(path: Path) -> bool:
    try:
        with connect(path, 1):
            pass
    except OSError:
        return False
    return True


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


async def start_server(handler: Handler, path: Path) -> asyncio.Server:
    """Listen at ``path`` (an old endpoint there must already be cleared by the caller)."""
    path = Path(path)
    if not is_tcp(path):
        old_umask = os.umask(0o177)  # the socket is created 0600 from the start
        try:
            server = await asyncio.start_unix_server(handler, path=str(path))
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        return server

    token = secrets.token_hex(16)
    expected = token.encode()

    async def checked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT_S)
        except (TimeoutError, ConnectionError, asyncio.LimitOverrunError, ValueError):
            line = b""
        if not hmac.compare_digest(line.rstrip(b"\r\n"), expected):
            writer.close()
            return
        await handler(reader, writer)

    server = await asyncio.start_server(checked, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _write_private(path, json.dumps({"port": port, "token": token}))
    return server
