"""Pulse digests — proactive briefings the operator can schedule or request.

Builds a short human-readable digest from pending approvals, failed tasks,
cron health, budget, and recent skills — then optionally delivers via gateway.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from . import config as cfg
from .logging_util import get_logger

if TYPE_CHECKING:
    from .daemon import Daemon

_log = get_logger("praxis.pulse")


def build_digest(daemon: "Daemon") -> dict[str, Any]:
    """Assemble a pulse digest dict + plain-text body."""
    daemon._ensure_agent()
    lines: list[str] = []
    lines.append(f"Praxis pulse · {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # Approvals
    pending = []
    try:
        pending = daemon.list_approvals() if hasattr(daemon, "list_approvals") else []
    except Exception:
        pending = []
    if not pending and daemon.agent is not None:
        pending = [
            {"approval_id": a.approval_id, "tool": a.tool, "preview": a.preview}
            for a in daemon.agent.broker.pending.values()
        ]
    lines.append(f"Approvals waiting: {len(pending)}")
    for a in pending[:5]:
        lines.append(f"  • {a.get('tool')} ({a.get('approval_id')})")
    if len(pending) > 5:
        lines.append(f"  … +{len(pending) - 5} more")

    # Tasks
    waiting = running = failed = 0
    if daemon.manager is not None:
        for t in daemon.manager.list(limit=50):
            st = getattr(t, "status", "") or ""
            if st == "waiting_approval":
                waiting += 1
            elif st in ("running", "queued", "pending"):
                running += 1
            elif st == "failed":
                failed += 1
    lines.append(f"Tasks: {running} active, {waiting} held, {failed} failed")

    # Budget
    try:
        b = daemon.budget_status()
        if b.get("limit_usd"):
            lines.append(
                f"Budget: ${b.get('spent_usd', 0):.4f} / ${b.get('limit_usd', 0):.2f}"
                + ("  ⚠ OVER" if b.get("over") else "")
            )
    except Exception:
        pass

    # Persona
    try:
        from .persona import load_persona
        p = load_persona()
        if p.get("display_name"):
            lines.append(f"For: {p['display_name']}" + (f" ({p['role']})" if p.get("role") else ""))
    except Exception:
        pass

    # Skills count
    try:
        if daemon.agent and getattr(daemon.agent, "skills", None):
            n = len(list(daemon.agent.skills.list() or []))
            lines.append(f"Skills installed: {n}")
    except Exception:
        pass

    body = "\n".join(lines)
    return {
        "ts": time.time(),
        "text": body,
        "approvals": len(pending),
        "tasks_waiting": waiting,
        "tasks_failed": failed,
    }


def deliver_digest(daemon: "Daemon", target: str | None = None) -> dict:
    """Build and send a pulse digest to the configured preferred channel."""
    dig = build_digest(daemon)
    from .persona import load_persona
    from .gateways import deliver
    p = load_persona()
    dest = target or p.get("preferred_channel") or ""
    if dest in ("", "dashboard", "local"):
        # Record only
        dig["delivered"] = False
        dig["detail"] = "local-only (set persona.preferred_channel to telegram/slack)"
        return dig
    res = deliver(dest if ":" in dest or dest in ("telegram", "slack", "discord", "ntfy", "webhook")
                  else f"{dest}", dig["text"])
    dig["delivered"] = res.ok
    dig["detail"] = res.detail
    dig["channel"] = res.channel
    if not res.ok:
        _log.warning("pulse deliver failed: %s", res.detail)
    return dig


def channel_status() -> dict:
    g = (cfg.load_config().get("agents") or {}).get("gateways") or {}
    return {
        "telegram": {
            "configured": bool((g.get("telegram") or {}).get("bot_token")),
            "enabled": bool((g.get("telegram") or {}).get("enabled")),
        },
        "slack": {
            "configured": bool((g.get("slack") or {}).get("webhook_url")
                               or (g.get("slack") or {}).get("bot_token")),
            "enabled": bool((g.get("slack") or {}).get("enabled")),
        },
    }
