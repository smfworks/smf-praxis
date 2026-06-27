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


def test_ws_connect_client_server_roundtrip():
    import socket
    import threading

    from hybridagent.wsutil import accept_key, ws_connect

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        rf = conn.makefile("rb")
        req = b""
        while not req.endswith(b"\r\n\r\n"):
            line = rf.readline()
            if not line:
                break
            req += line
        key = ""
        for line in req.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        conn.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\nSec-WebSocket-Accept: "
             + accept_key(key) + "\r\n\r\n").encode())
        ws = WebSocketConn(rf, conn.makefile("wb"))  # server side: unmasked
        op, data = ws.recv()                          # read the masked client frame
        ws.send(data, op)                             # echo it back
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        client = ws_connect(f"ws://127.0.0.1:{port}/realtime")
        client.send_text("ping-pong")
        op, data = client.recv()
        assert op == OP_TEXT and data == b"ping-pong"
        client.close()
    finally:
        thread.join(timeout=5)
        srv.close()
