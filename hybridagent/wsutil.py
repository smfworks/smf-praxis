"""Minimal RFC 6455 WebSocket server transport (stdlib only).

The daemon runs on ``http.server`` which has no WebSocket support, so realtime
voice would otherwise need a third-party dependency. This hand-rolled transport
keeps Praxis dependency-free: the handshake helpers compute the accept key and a
:class:`WebSocketConn` reads masked client frames and writes unmasked server
frames over the hijacked socket file objects.

Only what the realtime bridge needs is implemented: single (unfragmented) text/
binary frames, ping/pong, and close. That is sufficient for a JSON event channel
with base64 audio payloads.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
from typing import Any
from urllib.parse import urlparse

# Magic GUID from RFC 6455 used to derive Sec-WebSocket-Accept.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept value for a client's key."""
    digest = hashlib.sha1((client_key + _WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def is_ws_upgrade(headers) -> bool:
    """True when the request headers ask to upgrade to WebSocket."""
    upgrade = (headers.get("Upgrade", "") or "").lower()
    connection = (headers.get("Connection", "") or "").lower()
    return (upgrade == "websocket" and "upgrade" in connection
            and bool(headers.get("Sec-WebSocket-Key")))


class WebSocketConn:
    """A framed WebSocket connection over rfile/wfile (the hijacked socket)."""

    def __init__(self, rfile: Any, wfile: Any, mask: bool = False) -> None:
        self.rfile = rfile
        self.wfile = wfile
        self.mask = mask
        self.open = True

    def _read_exactly(self, n: int) -> bytes:
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                return b""  # EOF / peer closed
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv(self) -> tuple[int, bytes] | None:
        """Read one frame. Returns ``(opcode, payload)`` or ``None`` on close/EOF."""
        head = self._read_exactly(2)
        if len(head) < 2:
            return None
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            ext = self._read_exactly(2)
            if len(ext) < 2:
                return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = self._read_exactly(8)
            if len(ext) < 8:
                return None
            length = int.from_bytes(ext, "big")
        mask = self._read_exactly(4) if masked else b""
        if masked and len(mask) < 4:
            return None
        data = self._read_exactly(length) if length else b""
        if length and len(data) < length:
            return None
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def send(self, data: bytes, opcode: int = OP_TEXT) -> None:
        header = bytearray([0x80 | opcode])  # FIN + opcode
        mask_bit = 0x80 if self.mask else 0
        n = len(data)
        if n < 126:
            header.append(mask_bit | n)
        elif n < 65536:
            header.append(mask_bit | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(mask_bit | 127)
            header += n.to_bytes(8, "big")
        if self.mask:  # client -> server frames must be masked
            key = os.urandom(4)
            header += key
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        self.wfile.write(bytes(header) + data)
        self.wfile.flush()

    def send_text(self, text: str) -> None:
        self.send(text.encode("utf-8"), OP_TEXT)

    def send_bytes(self, data: bytes) -> None:
        self.send(data, OP_BINARY)

    def pong(self, data: bytes = b"") -> None:
        self.send(data, OP_PONG)

    def close(self) -> None:
        if not self.open:
            return
        try:
            self.send(b"", OP_CLOSE)
        except OSError:
            pass
        self.open = False


def ws_connect(url: str, headers: dict | None = None,
               timeout: float = 30.0) -> WebSocketConn:
    """Open a client WebSocket (ws:// or wss://) and return a masked
    :class:`WebSocketConn`. Raises ``RuntimeError`` if the handshake fails."""
    parsed = urlparse(url)
    secure = parsed.scheme == "wss"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if secure else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    sock: Any = socket.create_connection((host, port), timeout=timeout)
    if secure:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    request = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}",
               "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    for hkey, hval in (headers or {}).items():
        request.append(f"{hkey}: {hval}")
    sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline()
    if b" 101" not in status:
        raise RuntimeError(f"WebSocket handshake failed: {status!r}")
    while True:  # consume the remaining response headers
        line = rfile.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return WebSocketConn(rfile, sock.makefile("wb"), mask=True)
