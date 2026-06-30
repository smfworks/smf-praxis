"""Prebuilt MCP server presets — one-command integration for well-known servers.

A preset is a ready-to-use ``agents.mcp.servers`` entry so an operator can wire a
hosted MCP server without hand-writing JSON. The flagship preset is **xAI's Docs
MCP** (a keyless, read-only Streamable-HTTP server that exposes the xAI/Grok
documentation as governed tools), but the registry is open for more.

Enable from the CLI with ``praxis mcp --enable-preset xai-docs`` or
programmatically via :func:`enable_preset`. Presets are merged into config under
``agents.mcp.servers.<name>`` and immediately picked up by the governed loop.
"""
from __future__ import annotations

from . import config as cfg

# name -> server-config block (the same shape load_mcp_tools consumes).
PRESETS: dict[str, dict] = {
    "xai-docs": {
        "url": "https://docs.x.ai/api/mcp",
        "enabled": True,
        "description": "xAI / Grok documentation MCP (keyless, read-only): "
                       "search_docs, get_doc_page, list_doc_pages.",
        # All tools are read-only; pin them to READ so they run autonomously
        # under the broker even if a future tool name looks consequential.
        "risk": {
            "search_docs": "read",
            "get_doc_page": "read",
            "list_doc_pages": "read",
            "get_llms_txt": "read",
        },
    },
    "deepwiki": {
        "url": "https://mcp.deepwiki.com/mcp",
        "enabled": True,
        "description": "DeepWiki MCP (read-only): query open-source repo docs.",
    },
}


def preset_names() -> list[str]:
    return sorted(PRESETS)


def get_preset(name: str) -> dict | None:
    p = PRESETS.get(name)
    return dict(p) if p else None


def enable_preset(name: str) -> dict:
    """Merge a named preset into ``agents.mcp.servers`` in praxis.json.

    Returns a summary dict. Idempotent: re-enabling overwrites the same entry
    rather than duplicating it. Never touches other configured servers.
    """
    preset = get_preset(name)
    if preset is None:
        return {"error": f"unknown preset '{name}'. "
                         f"Available: {', '.join(preset_names())}"}
    conf = cfg.load_config()
    mcp = conf.setdefault("agents", {}).setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    servers[name] = preset
    cfg.save_config(conf)
    return {"enabled": name, "config": preset}


def disable_preset(name: str) -> dict:
    """Disable (mark enabled=false) a configured preset without deleting it."""
    conf = cfg.load_config()
    servers = (conf.get("agents", {}).get("mcp", {}).get("servers", {}) or {})
    if name not in servers:
        return {"error": f"server '{name}' is not configured"}
    servers[name]["enabled"] = False
    cfg.save_config(conf)
    return {"disabled": name}
