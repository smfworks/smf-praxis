"""Authenticated Phase 2 workspace HTTP integration contracts."""

import json
import socket
import urllib.error
import urllib.request

from hybridagent.daemon import Daemon
from hybridagent.llm import LLMClient
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store


def request(url, *, method="GET", body=None, headers=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_workspace_routes_and_board_are_isolated_end_to_end(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, admin = organizations.bootstrap("Practice", "admin@example.com")
    other_org, other_admin = organizations.bootstrap("Other", "other@example.com")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    daemon = Daemon(store=store, llm=LLMClient(mode="mock"), status_port=port)
    daemon._start_status_server()
    base = f"http://127.0.0.1:{port}/api/v1"

    def session(user_id, organization_id):
        status, payload = request(
            f"{base}/auth/session", method="POST",
            body={"user_id": user_id, "organization_id": organization_id})
        assert status == 201
        # Session endpoint's cookie must be read from a real response; issue directly
        # here to keep this helper independent of urllib's header elision.
        from hybridagent.authn import SessionManager
        issued = SessionManager(store).issue(user_id, organization_id)
        return (f"praxis_session={issued.token}", issued.csrf_token)

    try:
        cookie, csrf = session(admin.user_id, organization.organization_id)
        other_cookie, other_csrf = session(
            other_admin.user_id, other_org.organization_id)
        auth = {"Cookie": cookie, "X-CSRF-Token": csrf}
        other_auth = {"Cookie": other_cookie, "X-CSRF-Token": other_csrf}

        workspace_ids = []
        for identifier in ("MAT-1", "MAT-2"):
            status, payload = request(
                f"{base}/workspaces", method="POST",
                body={"human_identifier": identifier, "kind": "matter",
                      "title": identifier}, headers=auth)
            assert status == 201
            workspace_ids.append(payload["data"]["workspace"]["workspace_id"])

        status, payload = request(f"{base}/workspaces", headers={"Cookie": cookie})
        assert status == 200
        assert [item["human_identifier"] for item in payload["data"]["items"]] == [
            "MAT-1", "MAT-2"]

        for workspace_id, title in zip(workspace_ids, ("Only one", "Only two"),
                                       strict=True):
            status, payload = request(
                f"{base}/board/cards", method="POST", body={"title": title},
                headers={**auth, "X-Praxis-Workspace-ID": workspace_id,
                         "Idempotency-Key": "same-key"})
            assert status == 201
            assert payload["data"]["card"]["workspace_id"] == workspace_id

        for workspace_id, expected in zip(
                workspace_ids, ("Only one", "Only two"), strict=True):
            status, payload = request(
                f"{base}/board/cards",
                headers={"Cookie": cookie,
                         "X-Praxis-Workspace-ID": workspace_id})
            assert status == 200
            assert [item["title"] for item in payload["data"]["items"]] == [expected]

        status, payload = request(
            f"{base}/board/cards", headers={"Cookie": cookie})
        assert status == 400 and payload["error"]["code"] == "workspace_required"

        status, legacy = request(f"http://127.0.0.1:{port}/api/board")
        assert status == 200 and legacy["cards"] == []
        owned_card_id = store.list_cards(
            organization_id=organization.organization_id,
            workspace_id=workspace_ids[0])[0]["card_id"]
        for suffix, body in (
                ("move", {"card_id": owned_card_id, "lane": "done"}),
                ("run", {"card_id": owned_card_id}),
                ("delete", {"card_id": owned_card_id})):
            status, legacy_result = request(
                f"http://127.0.0.1:{port}/api/board/{suffix}",
                method="POST", body=body)
            assert status == 200
            assert legacy_result.get("error") == "card not found" or not legacy_result.get(
                "deleted")
        owned = store.get_card(
            owned_card_id, workspace_ids[0], organization.organization_id)
        assert owned is not None and owned["lane"] == "backlog"

        status, payload = request(
            f"{base}/board/cards",
            headers={"Cookie": other_cookie,
                     "X-Praxis-Workspace-ID": workspace_ids[0]})
        assert status == 404 and payload["error"]["code"] == "workspace_not_found"

        scope_headers = {**auth, "X-Praxis-Workspace-ID": workspace_ids[0]}
        status, payload = request(
            f"{base}/workspace/timeline", method="POST",
            body={"event_type": "note", "summary": "Workspace one event"},
            headers=scope_headers)
        assert status == 201 and payload["data"]["event"]["sequence"] == 1
        status, payload = request(
            f"{base}/workspace/timeline",
            headers={"Cookie": cookie,
                     "X-Praxis-Workspace-ID": workspace_ids[1]})
        assert status == 200 and payload["data"]["items"] == []

        status, payload = request(
            f"{base}/workspace/rooms", method="POST",
            body={"name": "Expert review", "permissions": ["read_shared"]},
            headers=scope_headers)
        assert status == 201
        assert payload["data"]["room"]["workspace_id"] == workspace_ids[0]
        status, payload = request(
            f"{base}/workspace/rooms", method="POST",
            body={"name": "Danger", "permissions": ["execute_tool"]},
            headers=scope_headers)
        assert status == 400 and payload["error"]["code"] == "invalid_external_room"
        status, payload = request(
            f"{base}/workspace/rooms", method="POST",
            body={"name": "x" * (70 * 1024)}, headers=scope_headers)
        assert status == 413 and payload["error"]["code"] == "payload_too_large"

        status, payload = request(
            f"{base}/workspaces", method="POST",
            body={"human_identifier": "OTH-1", "kind": "matter", "title": "Other"},
            headers=other_auth)
        assert status == 201
    finally:
        daemon._stop_status_server()
