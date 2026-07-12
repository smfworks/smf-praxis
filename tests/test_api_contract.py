"""Contract tests for the versioned Praxis HTTP API."""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from hybridagent import config as cfg
from hybridagent.api_contract import (
    API_VERSION,
    decode_cursor,
    encode_cursor,
    error_envelope,
    page_items,
    success_envelope,
)
from hybridagent.daemon import Daemon, _StatusHandler
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    daemon = Daemon(llm=LLMClient(mode="mock"), status_port=_free_port())
    daemon._start_status_server()
    try:
        yield f"http://127.0.0.1:{daemon.status_port}"
    finally:
        daemon._stop_status_server()


def _request(url: str, *, method: str = "GET", body: dict | None = None,
             headers: dict[str, str] | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, dict(response.headers), json.loads(response.read())


def test_envelopes_have_stable_shape():
    success = success_envelope({"answer": 42}, request_id="req-1")
    assert success == {
        "api_version": API_VERSION,
        "data": {"answer": 42},
        "meta": {"request_id": "req-1"},
    }
    failure = error_envelope("invalid_request", "Bad input", request_id="req-2",
                             details={"field": "title"})
    assert failure == {
        "api_version": API_VERSION,
        "error": {
            "code": "invalid_request",
            "message": "Bad input",
            "details": {"field": "title"},
        },
        "meta": {"request_id": "req-2"},
    }


def test_cursor_is_opaque_and_rejects_invalid_values():
    secret = b"test-secret"
    cursor = encode_cursor(25, secret=secret, snapshot="version-1")
    assert cursor != "25"
    assert decode_cursor(cursor, secret=secret, snapshot="version-1") == 25
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor("not-a-cursor", secret=secret, snapshot="version-1")
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(cursor, secret=b"wrong", snapshot="version-1")
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(cursor, secret=secret, snapshot="changed")


def test_page_items_returns_next_cursor():
    secret = b"test-secret"
    page, next_cursor = page_items(
        list(range(7)), limit=3, cursor=None, secret=secret, snapshot="v1")
    assert page == [0, 1, 2]
    assert decode_cursor(next_cursor, secret=secret, snapshot="v1") == 3
    final, final_cursor = page_items(
        list(range(7)), limit=10, cursor=next_cursor, secret=secret, snapshot="v1")
    assert final == [3, 4, 5, 6]
    assert final_cursor is None


def test_v1_board_uses_envelope_version_etag_and_pagination(live_daemon):
    for index in range(3):
        status, headers, payload = _request(
            f"{live_daemon}/api/v1/board/cards", method="POST",
            body={"title": f"Card {index}", "goal": f"Goal {index}"},
            headers={"Idempotency-Key": f"create-{index}"},
        )
        assert status == 201
        assert payload["api_version"] == "v1"
        assert payload["data"]["card"]["title"] == f"Card {index}"
        assert headers["X-API-Version"] == "v1"
        assert headers["ETag"].startswith('"')

    status, headers, payload = _request(
        f"{live_daemon}/api/v1/board/cards?limit=2"
    )
    assert status == 200
    assert len(payload["data"]["items"]) == 2
    assert payload["meta"]["next_cursor"]
    assert payload["meta"]["resource_version"]
    assert headers["X-API-Version"] == "v1"
    assert headers["ETag"].startswith('"')

    cursor = payload["meta"]["next_cursor"]
    _, _, second = _request(
        f"{live_daemon}/api/v1/board/cards?limit=2&cursor={cursor}"
    )
    assert len(second["data"]["items"]) == 1
    assert second["meta"]["next_cursor"] is None


def test_v1_cursor_rejects_mutated_board_snapshot(live_daemon):
    for index in range(3):
        _request(
            f"{live_daemon}/api/v1/board/cards", method="POST",
            body={"title": f"Initial {index}"},
        )
    _, _, first = _request(f"{live_daemon}/api/v1/board/cards?limit=2")
    cursor = first["meta"]["next_cursor"]
    _request(
        f"{live_daemon}/api/v1/board/cards", method="POST",
        body={"title": "Mutation"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(
            f"{live_daemon}/api/v1/board/cards?limit=2&cursor={cursor}", timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 400
    assert payload["error"]["code"] == "invalid_request"


def test_v1_etag_supports_conditional_get(live_daemon):
    _, headers, _ = _request(f"{live_daemon}/api/v1/board/cards")
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards",
        headers={"If-None-Match": headers["ETag"]},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 304
    assert caught.value.headers["X-API-Version"] == "v1"


def test_v1_create_is_idempotent(live_daemon):
    headers = {"Idempotency-Key": "same-operation"}
    first = _request(
        f"{live_daemon}/api/v1/board/cards", method="POST",
        body={"title": "One", "goal": "Do one"}, headers=headers,
    )
    second = _request(
        f"{live_daemon}/api/v1/board/cards", method="POST",
        body={"title": "One", "goal": "Do one"}, headers=headers,
    )
    assert first[2]["data"]["card"]["card_id"] == second[2]["data"]["card"]["card_id"]
    assert second[1]["Idempotency-Replayed"] == "true"


def test_v1_concurrent_duplicates_create_one_card(live_daemon):
    barrier = threading.Barrier(5)
    responses: list[tuple] = []
    errors: list[Exception] = []

    def create() -> None:
        try:
            barrier.wait(timeout=5)
            responses.append(_request(
                f"{live_daemon}/api/v1/board/cards", method="POST",
                body={"title": "Concurrent", "goal": "Do once"},
                headers={"Idempotency-Key": "concurrent-operation"},
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(responses) == 5
    card_ids = {response[2]["data"]["card"]["card_id"] for response in responses}
    assert len(card_ids) == 1
    _, _, board = _request(f"{live_daemon}/api/v1/board/cards")
    assert len(board["data"]["items"]) == 1


def test_v1_idempotency_key_rejects_different_payload(live_daemon):
    headers = {"Idempotency-Key": "reused-key"}
    _request(
        f"{live_daemon}/api/v1/board/cards", method="POST",
        body={"title": "Original"}, headers=headers,
    )
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards",
        data=json.dumps({"title": "Changed"}).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 409
    assert payload["error"]["code"] == "idempotency_conflict"


def test_v1_rejects_oversized_idempotency_key(live_daemon):
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards",
        data=json.dumps({"title": "Bounded"}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": "x" * 201},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 400
    assert payload["error"]["code"] == "invalid_request"
    assert "at most 200" in payload["error"]["message"]


def test_v1_rejects_oversized_json_body(live_daemon):
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards",
        data=json.dumps({"title": "x" * (64 * 1024)}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 413
    assert payload["api_version"] == "v1"
    assert payload["error"]["code"] == "payload_too_large"


def test_v1_read_auth_uses_structured_envelope(live_daemon, monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(_StatusHandler, "_is_loopback", lambda self: False)
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{live_daemon}/api/v1/board/cards", timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 401
    assert payload["api_version"] == "v1"
    assert payload["error"]["code"] == "unauthorized"

    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards",
        headers={"Authorization": "Bearer test-token"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_v1_errors_are_structured(live_daemon):
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 400
    assert payload["api_version"] == "v1"
    assert payload["error"]["code"] == "invalid_request"
    assert payload["error"]["message"]
    assert payload["meta"]["request_id"]


@pytest.mark.parametrize("body", [b"[]", b"null", b'"text"', b"42"])
def test_v1_non_object_json_uses_structured_client_error(live_daemon, body):
    request = urllib.request.Request(
        f"{live_daemon}/api/v1/board/cards", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    payload = json.loads(caught.value.read())
    assert caught.value.code == 400
    assert payload["api_version"] == "v1"
    assert payload["error"]["code"] == "invalid_request"


def test_idempotency_receipt_survives_daemon_restart(tmp_path):
    store_path = tmp_path / "praxis.db"
    store1 = Store(store_path)
    daemon1 = Daemon(store=store1, llm=LLMClient(mode="mock"))
    first, replayed, conflict = daemon1.api_idempotent_board_create(
        "restart-key", "fingerprint", "Persistent", "Persistent goal")
    assert not replayed and not conflict

    store2 = Store(store_path)
    daemon2 = Daemon(store=store2, llm=LLMClient(mode="mock"))
    second, replayed, conflict = daemon2.api_idempotent_board_create(
        "restart-key", "fingerprint", "Persistent", "Persistent goal")
    assert replayed and not conflict
    assert first["card"]["card_id"] == second["card"]["card_id"]
    assert len(store2.list_cards()) == 1


def test_idempotency_is_atomic_across_daemon_instances(tmp_path):
    store_path = tmp_path / "shared.db"
    daemons = [
        Daemon(store=Store(store_path), llm=LLMClient(mode="mock"))
        for _ in range(8)
    ]
    barrier = threading.Barrier(len(daemons))
    responses: list[tuple] = []
    errors: list[Exception] = []

    def create(daemon: Daemon) -> None:
        try:
            barrier.wait(timeout=5)
            responses.append(daemon.api_idempotent_board_create(
                "cross-process-key", "same-fingerprint", "Atomic", "Create once"))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create, args=(daemon,)) for daemon in daemons]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert len(responses) == len(daemons)
    assert sum(replayed for _, replayed, _ in responses) == len(daemons) - 1
    assert not any(conflict for _, _, conflict in responses)
    assert len({result["card"]["card_id"] for result, _, _ in responses}) == 1
    assert len(Store(store_path).list_cards()) == 1


def test_v1_unexpected_failure_keeps_envelope_and_hides_exception(
    live_daemon, monkeypatch,
):
    def explode(self):
        raise RuntimeError("sensitive-internal-detail")

    monkeypatch.setattr(Daemon, "board_list", explode)
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{live_daemon}/api/v1/board/cards", timeout=10)
    raw = caught.value.read().decode()
    payload = json.loads(raw)
    assert caught.value.code == 500
    assert payload["api_version"] == "v1"
    assert payload["error"]["code"] == "internal_error"
    assert "sensitive-internal-detail" not in raw


def test_legacy_board_alias_remains_unwrapped_and_is_deprecated(live_daemon):
    status, headers, payload = _request(f"{live_daemon}/api/board")
    assert status == 200
    assert "cards" in payload and "data" not in payload
    assert headers["Deprecation"] == "true"
    assert "successor-version" in headers["Link"]
