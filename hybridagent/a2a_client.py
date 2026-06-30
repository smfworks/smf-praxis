"""A2A client — call other autonomous agents over HTTP (Phase A / G4).

Praxis already *exposes* an A2A surface (``GET /api/agent/card`` advertises
capabilities; ``POST /api/agent/run`` executes a goal under governance). This is
the complementary *client*: it lets a Praxis agent discover and delegate to
*other* agents (another Praxis node, or any agent that speaks the same simple
HTTP contract).

Contract (intentionally minimal + matching Praxis's own server):
* discovery: ``GET <base>/api/agent/card`` -> ``{name, capabilities, tools, ...}``
* invocation: ``POST <base>/api/agent/run`` with ``{"goal": ...}`` ->
  ``{"summary"/"status"/...}``

Stdlib ``urllib`` only. Remote agents are **untrusted**: the calling tool is
SEND-risk so the broker holds outbound agent calls for approval, and registered
remote agents live under ``agents.a2a.peers`` with ``${ENV}`` header substitution
for auth (same pattern as the MCP HTTP transport).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field

from .logging_util import get_logger

_log = get_logger("praxis.a2a")

# Cap on an A2A peer's response body — peers are untrusted; refuse to buffer an
# unbounded body that could exhaust memory. 8 MiB is generous for a goal result.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _expand_env(value):
    def _sub(s: str) -> str:
        return re.sub(r"\$\{([A-Z0-9_]+)\}",
                      lambda m: os.environ.get(m.group(1), ""), s)
    if isinstance(value, str):
        return _sub(value)
    if isinstance(value, dict):
        return {k: (_sub(v) if isinstance(v, str) else v) for k, v in value.items()}
    return value


@dataclass
class AgentPeer:
    name: str
    base_url: str
    headers: dict = field(default_factory=dict)


class A2AClient:
    """Minimal HTTP client for the Praxis A2A contract."""

    def __init__(self, base_url: str, headers: dict | None = None,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        expanded = _expand_env(headers or {})
        self.headers = expanded if isinstance(expanded, dict) else {}
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        h = {"Accept": "application/json", **self.headers}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            # Cap the response body: a remote A2A peer is untrusted and could
            # return an arbitrarily large body to exhaust memory. Read one byte
            # past the limit so we can detect (and reject) an oversized response.
            raw_bytes = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw_bytes) > _MAX_RESPONSE_BYTES:
            raise ValueError(
                f"A2A response exceeds {_MAX_RESPONSE_BYTES} bytes (peer={self.base_url})")
        raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"raw": raw[:10000]}

    def card(self) -> dict:
        """Discover the remote agent's capabilities."""
        return self._request("GET", "/api/agent/card")

    def run(self, goal: str, max_replans: int = 1) -> dict:
        """Ask the remote agent to execute a goal under its own governance."""
        return self._request("POST", "/api/agent/run",
                             {"goal": goal, "max_replans": max_replans})


def _peers_config() -> dict:
    from . import config as cfg
    return cfg.load_config().get("agents", {}).get("a2a", {}).get("peers", {}) or {}


def get_peer(name: str) -> AgentPeer | None:
    block = _peers_config().get(name)
    if not block:
        return None
    return AgentPeer(name=name, base_url=block.get("url", ""),
                    headers=block.get("headers", {}) or {})


def list_peers() -> list[str]:
    return sorted(_peers_config())


def resolve_target(target: str) -> AgentPeer | None:
    """Resolve a call target: a registered peer name, or a bare http(s) URL."""
    target = (target or "").strip()
    if not target:
        return None
    if target.startswith("http://") or target.startswith("https://"):
        return AgentPeer(name=target, base_url=target)
    return get_peer(target)


def call_agent(target: str, goal: str, timeout: float = 60.0) -> dict:
    """Discover (best-effort) and invoke a remote agent. Never raises."""
    peer = resolve_target(target)
    if peer is None:
        return {"error": f"unknown agent peer '{target}' "
                         f"(configure agents.a2a.peers, or pass a URL)"}
    client = A2AClient(peer.base_url, headers=peer.headers, timeout=timeout)
    try:
        return client.run(goal)
    except Exception as exc:
        _log.warning("a2a call to %s failed: %s", peer.base_url, exc)
        return {"error": f"agent call failed: {exc}"}
