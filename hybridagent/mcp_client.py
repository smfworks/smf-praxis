"""Dependency-free MCP (Model Context Protocol) client — governed external tools.

``mcp_adapter`` already exposes Praxis tools *as* an MCP server, but it depends on
the third-party ``mcp`` package. This module is the complementary, **stdlib-only**
*client*: it speaks JSON-RPC 2.0 over a newline-delimited stdio transport so a
Praxis agent can consume tools from any external MCP server with **no extra
dependencies**.

The governance point is the whole point: external MCP tools are **untrusted**.
Each discovered tool is risk-classified (from its MCP annotations, then name
heuristics, then an optional per-tool config override) and wrapped as an ordinary
Praxis :class:`~hybridagent.tools.Tool`, so it flows through the same broker —
read tools run autonomously, send/destructive tools are **held for approval**.

A tiny reference echo server (:func:`serve_stdio` + :func:`echo_handler`, also
runnable via ``python -m hybridagent.mcp_client``) is included so the transport
can be exercised end-to-end without any external process.
"""
from __future__ import annotations

import json
import queue
import re
import threading
from typing import Any, Callable

from .broker import RiskClass
from .tools import Tool

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """A protocol-level or transport-level MCP failure."""


# --------------------------------------------------------------------- codec
def encode_message(obj: dict) -> bytes:
    """Serialise one JSON-RPC message as a single newline-terminated line."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_message(line: bytes) -> dict | None:
    """Parse one line into a JSON-RPC message, or ``None`` if blank/invalid."""
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


# ----------------------------------------------------------------- transport
class StdioTransport:
    """Newline-delimited JSON-RPC over a pair of binary streams.

    A background thread reads responses and routes them to the waiting caller by
    request id, so :meth:`request` is a simple blocking call with a timeout that
    cannot hang the agent forever.
    """

    def __init__(self, reader: Any, writer: Any, proc: Any = None) -> None:
        self._reader = reader
        self._writer = writer
        self._proc = proc
        self._next_id = 0
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            for line in iter(self._reader.readline, b""):
                msg = decode_message(line)
                if msg is None:
                    continue
                mid = msg.get("id")
                if isinstance(mid, int) and ("result" in msg or "error" in msg):
                    with self._plock:
                        waiter = self._pending.pop(mid, None)
                    if waiter is not None:
                        waiter.put(msg)
                # Server-initiated requests/notifications are ignored: this client
                # exposes no tools and grants no sampling.
        except (ValueError, OSError):
            pass

    def _send(self, obj: dict) -> None:
        data = encode_message(obj)
        with self._wlock:
            self._writer.write(data)
            self._writer.flush()

    def request(self, method: str, params: dict | None = None,
                timeout: float = 20.0) -> dict:
        with self._plock:
            self._next_id += 1
            mid = self._next_id
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[mid] = waiter
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params or {}})
        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._plock:
                self._pending.pop(mid, None)
            raise MCPError(f"timed out waiting for response to {method!r}") from None
        if "error" in msg:
            err = msg["error"]
            detail = err.get("message", err) if isinstance(err, dict) else err
            raise MCPError(f"{method} failed: {detail}")
        result = msg.get("result", {})
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                pass

    @classmethod
    def spawn(cls, command: list[str], env: dict | None = None,
              cwd: str | None = None) -> "StdioTransport":
        import os
        import subprocess
        full_env = {**os.environ, **env} if env else None
        proc = subprocess.Popen(  # noqa: S603 - command comes from trusted config
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=full_env, cwd=cwd, bufsize=0)
        return cls(proc.stdout, proc.stdin, proc=proc)


# -------------------------------------------------------------------- client
class MCPClient:
    """A minimal MCP client over a :class:`StdioTransport` (or any compatible)."""

    def __init__(self, transport: Any, *, client_name: str = "praxis",
                 client_version: str = "0.1.0") -> None:
        self.transport = transport
        self.client_name = client_name
        self.client_version = client_version
        self.server_info: dict = {}

    def initialize(self) -> dict:
        result = self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        })
        self.server_info = result.get("serverInfo", {}) or {}
        self.transport.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools = self.transport.request("tools/list").get("tools", [])
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        result = self.transport.request(
            "tools/call", {"name": name, "arguments": arguments or {}})
        blocks = result.get("content", []) or []
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        if result.get("isError"):
            return f"ERROR: {text or 'the MCP tool reported an error'}"
        return text or "(no text output)"

    def close(self) -> None:
        self.transport.close()

    @classmethod
    def connect_stdio(cls, command: list[str], env: dict | None = None,
                      cwd: str | None = None, **kw: Any) -> "MCPClient":
        return cls(StdioTransport.spawn(command, env=env, cwd=cwd), **kw)


# ------------------------------------------------------------- risk mapping
_READ_HINTS = {"read", "get", "list", "search", "find", "fetch", "show", "view", "query"}
_DRAFT_HINTS = {"write", "create", "save", "draft", "append", "update", "edit"}
_SEND_HINTS = {"send", "post", "publish", "commit", "submit", "share", "email"}
_DESTRUCTIVE_HINTS = {"delete", "remove", "drop", "destroy", "kill", "purge"}

_RISK_BY_NAME = {
    "read": RiskClass.READ, "draft": RiskClass.DRAFT,
    "send": RiskClass.SEND, "destructive": RiskClass.DESTRUCTIVE,
}


def risk_for_tool(tool_def: dict, override: str | None = None) -> RiskClass:
    """Classify an MCP tool: explicit override, then annotations, then name."""
    if override:
        mapped = _RISK_BY_NAME.get(override.lower())
        if mapped is not None:
            return mapped
    ann = tool_def.get("annotations")
    if isinstance(ann, dict):
        if ann.get("readOnlyHint"):
            return RiskClass.READ
        if ann.get("destructiveHint"):
            return RiskClass.DESTRUCTIVE
    name = (tool_def.get("name") or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", name))
    if tokens & _DESTRUCTIVE_HINTS:
        return RiskClass.DESTRUCTIVE
    if tokens & _SEND_HINTS:
        return RiskClass.SEND
    if tokens & _DRAFT_HINTS:
        return RiskClass.DRAFT
    return RiskClass.READ


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def mcp_tools(client: MCPClient, server_name: str = "external",
              risk_overrides: dict | None = None) -> list[Tool]:
    """Discover an MCP server's tools and wrap each as a governed Praxis Tool."""
    overrides = {k.lower(): v for k, v in (risk_overrides or {}).items()}
    out: list[Tool] = []
    for td in client.list_tools():
        tname = td.get("name") or ""
        if not tname:
            continue
        risk = risk_for_tool(td, override=overrides.get(tname.lower()))
        schema = td.get("inputSchema") or {"type": "object", "properties": {}}

        def _run(_name: str = tname, **kwargs: Any) -> str:
            return client.call_tool(_name, kwargs)

        out.append(Tool(
            name=f"mcp_{server_name}_{_sanitize(tname)}", risk=risk,
            description=td.get("description") or f"MCP tool {tname} ({server_name})",
            run=_run, parameters=dict(schema)))
    return out


def load_mcp_tools(timeout: float = 20.0) -> "tuple[list[Tool], list[MCPClient]]":
    """Spawn every enabled ``agents.mcp.servers`` entry and adapt their tools.

    Returns ``(tools, clients)``; the caller owns closing the clients. A server
    that fails to start is skipped so one bad entry can't break the agent.
    """
    from . import config as cfg
    servers = (cfg.load_config().get("agents", {})
               .get("mcp", {}).get("servers", {}) or {})
    tools: list[Tool] = []
    clients: list[MCPClient] = []
    for name, sc in servers.items():
        if not sc.get("enabled", True):
            continue
        command = sc.get("command")
        if isinstance(command, str):
            command = [command, *sc.get("args", [])]
        if not command:
            continue
        client: MCPClient | None = None
        try:
            client = MCPClient.connect_stdio(command, env=sc.get("env"))
            client.initialize()
            tools.extend(mcp_tools(client, server_name=name,
                                   risk_overrides=sc.get("risk")))
            clients.append(client)
        except Exception:
            if client is not None:
                client.close()
            continue
    return tools, clients


def augment_registry_with_mcp(registry: Any, *,
                              allowlist: "set[str] | None" = None,
                              ) -> "tuple[list[Tool], list[MCPClient]]":
    """Load configured MCP servers, register their tools, and extend an allowlist.

    Registers every discovered tool in ``registry``, adds its name to
    ``allowlist`` (e.g. the broker policy's ``allowed_tools``) when given, and
    returns ``(tools, clients)``. Spawns nothing when no servers are configured,
    so it is a zero-cost no-op by default; the caller owns closing the clients.
    """
    tools, clients = load_mcp_tools()
    for tool in tools:
        registry.register(tool)
        if allowlist is not None:
            allowlist.add(tool.name)
    return tools, clients
# ----------------------------------------------------- reference echo server
def serve_stdio(handler: Callable[[str, dict], Any], reader: Any,
                writer: Any) -> None:
    """Minimal MCP server loop: dispatch requests through ``handler``.

    ``handler(method, params)`` returns the JSON-RPC ``result`` (or raises to
    produce an error). Notifications are consumed silently. Reused by tests and
    by ``python -m hybridagent.mcp_client``.
    """
    for line in iter(reader.readline, b""):
        msg = decode_message(line)
        if msg is None:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if not method:
            continue
        if mid is None:  # a notification — nothing to answer
            continue
        try:
            result = handler(method, msg.get("params") or {})
            resp = {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32000, "message": str(exc)}}
        writer.write(encode_message(resp))
        writer.flush()


_ECHO_TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete_record", "description": "Pretend to delete a record",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}},
                     "required": ["id"]},
     "annotations": {"destructiveHint": True}},
]


def echo_handler(method: str, params: dict) -> Any:
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "praxis-echo", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": _ECHO_TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}],
                    "isError": False}
        if name == "delete_record":
            return {"content": [{"type": "text", "text": f"deleted {args.get('id', '')}"}],
                    "isError": False}
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                "isError": True}
    raise ValueError(f"unknown method {method}")


if __name__ == "__main__":  # pragma: no cover - manual/integration entrypoint
    import sys
    serve_stdio(echo_handler, sys.stdin.buffer, sys.stdout.buffer)
