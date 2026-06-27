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
from typing import Any

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

    def __init__(self, rfile: Any, wfile: Any) -> None:
        self.rfile = rfile
        self.wfile = wfile
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
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += n.to_bytes(2, "big")
        else:
            header.append(127)
            header += n.to_bytes(8, "big")
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
