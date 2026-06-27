"""Tests for the hand-rolled WebSocket transport (RFC 6455)."""

import io

from hybridagent.wsutil import (
    OP_BINARY,
    OP_TEXT,
    WebSocketConn,
    accept_key,
    is_ws_upgrade,
)


def test_accept_key_rfc_vector():
    # The example key/accept pair from RFC 6455 section 1.3.
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def _client_frame(payload: bytes, opcode: int = OP_TEXT,
                  mask: bytes = b"\x01\x02\x03\x04") -> bytes:
    out = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out.append(0x80 | 126)
        out += n.to_bytes(2, "big")
    else:
        out.append(0x80 | 127)
        out += n.to_bytes(8, "big")
    out += mask
    out += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def test_recv_unmasks_client_frame():
    conn = WebSocketConn(io.BytesIO(_client_frame(b"hello")), io.BytesIO())
    op, data = conn.recv()
    assert op == OP_TEXT and data == b"hello"


def test_send_is_unmasked_and_roundtrips():
    out = io.BytesIO()
    WebSocketConn(io.BytesIO(), out).send_text("hi there")
    reader = WebSocketConn(io.BytesIO(out.getvalue()), io.BytesIO())
    op, data = reader.recv()
    assert op == OP_TEXT and data == b"hi there"


def test_large_payload_extended_length():
    payload = b"x" * 1000
    conn = WebSocketConn(io.BytesIO(_client_frame(payload, OP_BINARY)), io.BytesIO())
    op, data = conn.recv()
    assert op == OP_BINARY and data == payload


def test_recv_eof_returns_none():
    assert WebSocketConn(io.BytesIO(b""), io.BytesIO()).recv() is None


def test_is_ws_upgrade():
    assert is_ws_upgrade({"Upgrade": "websocket", "Connection": "Upgrade",
                          "Sec-WebSocket-Key": "abc"})
    assert not is_ws_upgrade({"Upgrade": "h2c", "Connection": "Upgrade"})
    assert not is_ws_upgrade({"Upgrade": "websocket", "Connection": "keep-alive"})
