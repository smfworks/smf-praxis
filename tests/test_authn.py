"""Secure professional session contracts."""
import json
import socket
import urllib.error
import urllib.request

from hybridagent.authn import SessionManager, session_token_from_cookie
from hybridagent.daemon import Daemon
from hybridagent.llm import LLMClient
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store


def setup_identity(tmp_path):
    store = Store(tmp_path / "praxis.db")
    directory = OrganizationDirectory(store)
    organization, user = directory.bootstrap("Practice", "admin@example.com")
    return store, organization, user


def test_session_secret_is_opaque_and_not_stored_verbatim(tmp_path):
    store, organization, user = setup_identity(tmp_path)
    sessions = SessionManager(store)
    issued = sessions.issue(user.user_id, organization.organization_id)
    assert issued.token and issued.csrf_token and issued.token != issued.csrf_token
    row = store._directory_one(
        "SELECT token_hash,csrf_hash FROM professional_sessions WHERE session_id=?",
        (issued.session_id,))
    assert issued.token not in row.values()
    assert issued.csrf_token not in row.values()
    assert sessions.authenticate(issued.token).user_id == user.user_id


def test_mutation_requires_matching_csrf(tmp_path):
    store, organization, user = setup_identity(tmp_path)
    sessions = SessionManager(store)
    issued = sessions.issue(user.user_id, organization.organization_id)
    assert sessions.authenticate(issued.token, mutation=True, csrf_token="wrong") is None
    assert sessions.authenticate(
        issued.token, mutation=True, csrf_token=issued.csrf_token) is not None


def test_session_expiry_and_device_revocation(tmp_path):
    store, organization, user = setup_identity(tmp_path)
    sessions = SessionManager(store)
    expired = sessions.issue(user.user_id, organization.organization_id,
                             ttl_seconds=-1, device_id="laptop")
    assert sessions.authenticate(expired.token) is None
    active = sessions.issue(user.user_id, organization.organization_id,
                            ttl_seconds=3600, device_id="laptop")
    assert sessions.revoke_device(user.user_id, "laptop") == 1
    assert sessions.authenticate(active.token) is None


def test_disabled_membership_invalidates_session(tmp_path):
    store, organization, user = setup_identity(tmp_path)
    sessions = SessionManager(store)
    issued = sessions.issue(user.user_id, organization.organization_id)
    store._directory_execute(
        "UPDATE organization_memberships SET status='disabled' "
        "WHERE organization_id=? AND user_id=?",
        (organization.organization_id, user.user_id))
    assert sessions.authenticate(issued.token) is None


def test_cookie_parser_is_strict_and_named():
    assert session_token_from_cookie("praxis_session=abc; other=x") == "abc"
    assert session_token_from_cookie("other=x") == ""
    assert session_token_from_cookie("not a cookie ;;;") == ""


def test_v1_session_cookie_requires_csrf_for_mutation(tmp_path):
    store, organization, user = setup_identity(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    daemon = Daemon(store=store, llm=LLMClient(mode="mock"), status_port=port)
    daemon._start_status_server()
    base = f"http://127.0.0.1:{port}"
    try:
        login = urllib.request.Request(
            f"{base}/api/v1/auth/session", method="POST",
            data=json.dumps({"user_id": user.user_id,
                             "organization_id": organization.organization_id,
                             "device_id": "browser"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(login, timeout=10) as response:
            payload = json.loads(response.read())["data"]
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            assert "HttpOnly" in response.headers["Set-Cookie"]
            assert "SameSite=Strict" in response.headers["Set-Cookie"]

        rejected = urllib.request.Request(
            f"{base}/api/v1/board/cards", method="POST",
            data=b'{"title":"Denied"}',
            headers={"Content-Type": "application/json", "Cookie": cookie})
        try:
            urllib.request.urlopen(rejected, timeout=10)
            raise AssertionError("mutation without CSRF unexpectedly succeeded")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        accepted = urllib.request.Request(
            f"{base}/api/v1/board/cards", method="POST",
            data=b'{"title":"Accepted"}',
            headers={"Content-Type": "application/json", "Cookie": cookie,
                     "X-CSRF-Token": payload["csrf_token"]})
        with urllib.request.urlopen(accepted, timeout=10) as response:
            assert response.status == 201
    finally:
        daemon._stop_status_server()


def test_professional_http_routes_enforce_tenant_and_role(tmp_path):
    from hybridagent.broker import RiskClass

    store = Store(tmp_path / "praxis.db")
    directory = OrganizationDirectory(store)
    org_a, admin_a = directory.bootstrap("A", "a@example.com")
    org_b, admin_b = directory.bootstrap("B", "b@example.com")
    reviewer = directory.create_user("reviewer@example.com")
    directory.add_membership(org_a.organization_id, reviewer.user_id,
                             roles=("reviewer",))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    daemon = Daemon(store=store, llm=LLMClient(mode="mock"), status_port=port)
    daemon._start_status_server()
    base = f"http://127.0.0.1:{port}"

    def session(user_id: str, organization_id: str) -> tuple[str, str]:
        req = urllib.request.Request(
            f"{base}/api/v1/auth/session", method="POST",
            data=json.dumps({"user_id": user_id,
                             "organization_id": organization_id}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.loads(response.read())["data"]
            return response.headers["Set-Cookie"].split(";", 1)[0], body["csrf_token"]

    def create(cookie: str, csrf: str, title: str) -> int:
        req = urllib.request.Request(
            f"{base}/api/v1/board/cards", method="POST",
            data=json.dumps({"title": title}).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie,
                     "X-CSRF-Token": csrf})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        cookie_a, csrf_a = session(admin_a.user_id, org_a.organization_id)
        cookie_b, csrf_b = session(admin_b.user_id, org_b.organization_id)
        cookie_r, csrf_r = session(reviewer.user_id, org_a.organization_id)
        assert create(cookie_a, csrf_a, "Only A") == 201
        assert create(cookie_b, csrf_b, "Only B") == 201
        assert create(cookie_r, csrf_r, "Reviewer denied") == 403

        for cookie, expected in ((cookie_a, "Only A"), (cookie_b, "Only B")):
            req = urllib.request.Request(
                f"{base}/api/v1/board/cards", headers={"Cookie": cookie})
            with urllib.request.urlopen(req, timeout=10) as response:
                items = json.loads(response.read())["data"]["items"]
            assert [item["title"] for item in items] == [expected]

        daemon._ensure_agent()
        assert daemon.agent is not None
        pending = daemon.agent.broker.authorize(
            "agent", "send_email", RiskClass.SEND,
            {"classification": "public", "connector": "public_web",
             "redacted": True}, organization_id=org_a.organization_id)
        assert pending.approval_id
        cross = urllib.request.Request(
            f"{base}/api/v1/approvals/{pending.approval_id}/approve", method="POST",
            data=b"{}", headers={"Content-Type": "application/json",
                                  "Cookie": cookie_b, "X-CSRF-Token": csrf_b})
        try:
            urllib.request.urlopen(cross, timeout=10)
            raise AssertionError("cross-tenant approval unexpectedly succeeded")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        daemon._stop_status_server()
