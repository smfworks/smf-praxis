"""Tests for the MCP adapter.

We avoid spinning real child processes by building the MCP server in-process and
exercising its request handlers directly. Client loading is verified with a
mock ``ClientSession``.
"""
from __future__ import annotations

import pytest

from hybridagent.broker import RiskClass
from hybridagent.mcp_adapter import (
    _make_sync_runner,
    annotations_from_risk,
    build_mcp_server,
    risk_from_annotations,
)
from hybridagent.tools import Tool, ToolRegistry, default_registry


@pytest.fixture
def empty_registry():
    return ToolRegistry()


@pytest.fixture
def echo_tool():
    return Tool(
        name="echo",
        risk=RiskClass.READ,
        description="Echo back the input.",
        run=lambda message: f"echo: {message}",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )


@pytest.mark.asyncio
async def test_mcp_server_lists_tools(empty_registry, echo_tool):
    empty_registry.register(echo_tool)
    server = build_mcp_server(empty_registry, name="test-praxis", version="0.0.1")
    # Trigger list_tools handler to populate the server's tool cache.
    from mcp.types import ListToolsRequest
    req = ListToolsRequest(method="tools/list")
    res = await server.request_handlers[ListToolsRequest](req)
    assert "echo" in server._tool_cache
    assert server._tool_cache["echo"].name == "echo"
    assert server._tool_cache["echo"].inputSchema["required"] == ["message"]


@pytest.mark.asyncio
async def test_mcp_server_calls_tool(empty_registry, echo_tool):
    empty_registry.register(echo_tool)
    server = build_mcp_server(empty_registry, name="test-praxis")
    # Prime the cache by listing tools.
    from mcp.types import CallToolRequest, ListToolsRequest
    await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    req = CallToolRequest(method="tools/call", params={"name": "echo", "arguments": {"message": "hello"}})
    server_result = await server.request_handlers[CallToolRequest](req)
    result = server_result.root
    assert len(result.content) == 1
    assert result.content[0].text == "echo: hello"


def test_risk_class_mapping():
    assert risk_from_annotations("read_file", None) is RiskClass.READ
    assert risk_from_annotations("send_email", None) is RiskClass.SEND
    assert risk_from_annotations("delete_file", None) is RiskClass.DESTRUCTIVE
    assert risk_from_annotations("write_file", None) is RiskClass.DRAFT


class FakeAnnotations:
    readOnlyHint = True
    destructiveHint = False


class FakeAnnotationsDestructive:
    readOnlyHint = False
    destructiveHint = True


def test_risk_class_mapping_from_annotations():
    assert risk_from_annotations("any", FakeAnnotations()) is RiskClass.READ
    assert risk_from_annotations("any", FakeAnnotationsDestructive()) is RiskClass.DESTRUCTIVE


def test_annotations_from_risk_round_trip():
    ann = annotations_from_risk(RiskClass.READ, title="read")
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False
    ann = annotations_from_risk(RiskClass.SEND, title="send")
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True


def test_default_registry_mcp_mapping():
    """All default tools must have JSON schemas so they can be exposed via MCP."""
    reg = default_registry()
    for tool in reg.catalog():
        assert tool.parameters is not None, f"{tool.name} is missing a schema"
        assert "type" in tool.parameters, f"{tool.name} schema missing type"


def test_make_sync_runner():
    async def async_runner(kwargs):
        return f"got {kwargs['x']}"

    sync = _make_sync_runner(async_runner)
    assert sync(x=1) == "got 1"
