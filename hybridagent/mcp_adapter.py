"""MCP adapter for Praxis.

This module has two responsibilities:

1. **Server** (`mcp_server`) — expose the Praxis tool registry as an MCP server
   so Claude Desktop / Copilot / any MCP host can call Praxis tools. Tools are
   annotated with readOnlyHint/destructiveHint derived from the Praxis RiskClass.

2. **Client** (`MCPClient`) — let a PraxisAgent consume tools from external MCP
   servers. Discovered tools are wrapped as Praxis Tool instances and inherit a
   RiskClass mapping based on the server's tool annotations and name heuristics.

The dependency on the ``mcp`` package is optional; importing this module without
``mcp`` installed raises ImportError, but the rest of hybridagent keeps working.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .broker import RiskClass
from .tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from .agent import PraxisAgent


# --------------------------------------------------------------------- optional import
try:
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from mcp.server import Server as MCPServer
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        TextContent,
        ToolAnnotations,
    )
    from mcp.types import (
        Tool as MCPTool,
    )
    _HAS_MCP = True
except Exception:  # pragma: no cover - defensive when mcp is not installed
    _HAS_MCP = False


def _ensure_mcp() -> None:
    if not _HAS_MCP:
        raise ImportError(
            "MCP support requires the 'mcp' package. "
            "Install it with: pip install mcp"
        )


# -------------------------------------------------------- risk class mapping
_READ_HINTS = {"read", "get", "list", "search", "find", "fetch", "show", "view"}
_DRAFT_HINTS = {"write", "create", "save", "draft", "append", "update", "edit"}
_SEND_HINTS = {"send", "post", "publish", "commit", "submit", "share"}
_DESTRUCTIVE_HINTS = {"delete", "remove", "drop", "destroy", "kill"}


def risk_from_annotations(name: str, annotations: Any | None) -> RiskClass:
    """Best-effort RiskClass mapping from MCP ToolAnnotations + tool name."""
    if annotations is not None:
        if getattr(annotations, "readOnlyHint", False):
            return RiskClass.READ
        if getattr(annotations, "destructiveHint", False):
            return RiskClass.DESTRUCTIVE
    lowered = name.lower()
    for hint in _DESTRUCTIVE_HINTS:
        if hint in lowered:
            return RiskClass.DESTRUCTIVE
    for hint in _SEND_HINTS:
        if hint in lowered:
            return RiskClass.SEND
    for hint in _DRAFT_HINTS:
        if hint in lowered:
            return RiskClass.DRAFT
    return RiskClass.READ


def annotations_from_risk(risk: RiskClass, title: str = "") -> ToolAnnotations:
    """Map a Praxis RiskClass back to MCP ToolAnnotations hints."""
    _ensure_mcp()
    destructive = risk is RiskClass.DESTRUCTIVE or risk is RiskClass.SEND
    return ToolAnnotations(
        title=title or None,
        readOnlyHint=risk is RiskClass.READ,
        destructiveHint=destructive,
        idempotentHint=risk is RiskClass.READ,
        openWorldHint=risk in (RiskClass.READ, RiskClass.SEND),
    )


# ----------------------------------------------------------------- MCP server
def _tool_to_mcp(tool: Tool) -> MCPTool:
    _ensure_mcp()
    schema = dict(tool.parameters) if tool.parameters else {"type": "object"}
    return MCPTool(
        name=tool.name,
        description=tool.description,
        inputSchema=schema,
        annotations=annotations_from_risk(tool.risk, title=tool.name),
    )


def build_mcp_server(registry: ToolRegistry, name: str = "praxis",
                     version: str = "0.19.0") -> MCPServer:
    """Build an MCP server that advertises and runs every tool in ``registry``."""
    _ensure_mcp()
    server = MCPServer(name=name, version=version)

    @server.list_tools()
    async def list_tools() -> list[MCPTool]:
        return [_tool_to_mcp(t) for t in registry.catalog()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
        tool = registry.get(name)
        if not tool:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        args = arguments or {}
        try:
            result = tool.run(**args)
        except Exception as exc:  # noqa: BLE001 - surface as MCP text error
            return [TextContent(type="text", text=f"Error calling {name}: {exc}")]
        return [TextContent(type="text", text=str(result))]

    return server


async def run_stdio_server(registry: ToolRegistry | None = None,
                           name: str = "praxis") -> None:
    """Run the Praxis MCP server over stdio (used by ``praxis mcp``)."""
    _ensure_mcp()
    registry = registry or ToolRegistry()
    server = build_mcp_server(registry, name=name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ----------------------------------------------------------------- MCP client
class MCPClient:
    """Load tools from one or more external MCP servers into a Praxis registry.

    Example:

        client = MCPClient()
        reg = ToolRegistry()
        await client.load_server(reg, command="npx",
                                 args=["-y", "@modelcontextprotocol/server-time"],
                                 server_name="time")
    """

    def __init__(self) -> None:
        _ensure_mcp()
        self._sessions: dict[str, ClientSession] = {}

    async def load_server(
        self,
        registry: ToolRegistry,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        server_name: str = "external",
    ) -> list[str]:
        """Connect to a stdio MCP server and register its tools in ``registry``.

        Tool names are prefixed with ``mcp_{server_name}_`` to avoid collisions.
        Returns the list of registered names.
        """
        params = StdioServerParameters(command=command, args=args or [], env=env)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                registered: list[str] = []
                prefix = f"mcp_{server_name}_"
                for mcp_tool in result.tools:
                    praxis_name = f"{prefix}{mcp_tool.name.replace('-', '_').replace('.', '_')}"
                    risk = risk_from_annotations(mcp_tool.name,
                                                getattr(mcp_tool, "annotations", None))

                    async def _run(args: dict, sess=session, tname=mcp_tool.name) -> str:
                        try:
                            call_result = await sess.call_tool(tname, args)
                            texts = [c.text for c in call_result.content
                                     if getattr(c, "text", None)]
                            return "\n".join(texts) or "(no text output)"
                        except Exception as exc:  # noqa: BLE001
                            return f"[mcp {tname}] error: {exc}"

                    registry.register(Tool(
                        name=praxis_name,
                        risk=risk,
                        description=mcp_tool.description or f"MCP tool {mcp_tool.name}",
                        run=_make_sync_runner(_run),
                        parameters=dict(mcp_tool.inputSchema),
                    ))
                    registered.append(praxis_name)
                self._sessions[server_name] = session
                return registered


def _make_sync_runner(awaitable_runner):
    """Wrap an async tool runner so it can be called from sync Praxis Tool.run."""
    import asyncio

    def run(**kwargs) -> str:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable_runner(kwargs))
        # If we're already in an async context, schedule it; this is acceptable
        # for read tools but should be used carefully with SEND-class tools.
        return asyncio.run_coroutine_threadsafe(awaitable_runner(kwargs), loop).result()

    return run


# -------------------------------------------------------------- agent helpers
def agent_with_mcp_servers(agent: "PraxisAgent",
                           server_configs: list[dict[str, Any]]) -> "PraxisAgent":
    """Attach external MCP servers to an existing agent's registry.

    ``server_configs`` entries should contain ``command``, ``args``, ``env``,
    and ``name`` keys. This is synchronous convenience; for production use
    prefer awaiting ``MCPClient.load_server`` directly before constructing the
    agent.
    """
    import asyncio

    client = MCPClient()

    async def _load() -> None:
        for cfg in server_configs:
            await client.load_server(
                agent.registry,
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env"),
                server_name=cfg.get("name", "external"),
            )

    asyncio.run(_load())
    # Re-seed the broker allowlist with the newly discovered tools.
    agent.broker.policy.allowed_tools.update(agent.registry.names())
    return agent
