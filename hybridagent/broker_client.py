"""HTTP client for the OpenClaw M365 Access Broker (stdlib only).

The broker is a separate local control plane (Node service on 127.0.0.1:8787)
that gates every Microsoft Graph call. Praxis talks to it over its loopback
HTTP API:

    GET  /health                      -> { ok, mode, dryRun, requiredScopes }
    GET  /tools     (x-broker-key)    -> { ok, tools: [...] }
    POST /execute   (x-broker-key)    -> { ok, outcome, result, security? }
    POST /approve   (x-approver-key)  -> { ok, approvalId, expiresInMs }

Two keys are intentionally separate: the *agent* key reads/drafts/executes; the
*approver* key (held by the host UI — here, Praxis when the human approves) mints
single-use, tool-scoped approval tokens the agent could never mint itself.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class BrokerClient:
    base_url: str = "http://127.0.0.1:8787"
    broker_key: str | None = None
    approver_key: str | None = None
    timeout: float = 30.0
    last_draft_id: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "BrokerClient":
        return cls(
            base_url=os.environ.get("M365_BROKER_URL", "http://127.0.0.1:8787"),
            broker_key=os.environ.get("M365_BROKER_KEY"),
            approver_key=os.environ.get("M365_BROKER_APPROVER_KEY"),
        )

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, headers: dict,
                 body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "error": f"http_{e.code}"}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"broker_unreachable: {e.reason}"}

    # ------------------------------------------------------------- endpoints
    def health(self) -> dict:
        return self._request("GET", "/health", headers={})

    def list_tools(self) -> dict:
        return self._request("GET", "/tools", headers=self._agent_headers())

    def execute(self, tool: str, args: dict | None = None,
                approval_id: str | None = None) -> dict:
        body: dict = {"tool": tool, "args": args or {}}
        if approval_id:
            body["approvalId"] = approval_id
        return self._request("POST", "/execute", self._agent_headers(), body)

    def approve(self, tool: str, args: dict | None = None) -> dict:
        if not self.approver_key:
            return {"ok": False, "error": "no_approver_key"}
        return self._request("POST", "/approve", {"x-approver-key": self.approver_key},
                             {"tool": tool, "args": args or {}})

    # --------------------------------------------------------------- helpers
    def _agent_headers(self) -> dict:
        return {"x-broker-key": self.broker_key} if self.broker_key else {}
