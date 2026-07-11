"""Agent-oriented error messages (H07).

Formats the three-part error messages the harness course (L9/L10) calls
for: WHAT went wrong, WHY, and HOW TO FIX it. Bare messages like
"denied by policy hook" tell the agent nothing it can act on; the agent
loops. "denied by policy hook: the kill-switch is engaged. Wait for an
operator to reset it with `praxis kill-switch reset`." lets the agent
self-correct or surface a clear blocker.

This module is stdlib-only (no third-party imports) and is imported by
the governance modules that construct error/denial messages. The format
is deliberately grep-friendly so the H07 verification command and the
architecture checker can confirm the convention is in use.

Convention:
    from .errors import agent_error
    reason = agent_error(
        what="absolute paths not allowed",
        why="filesystem tools sandbox to PRAXIS_WORK_DIR for safety",
        fix="use a path relative to the work directory, e.g. 'data/file.txt'",
    )
"""
from __future__ import annotations


def agent_error(what: str, why: str = "", fix: str = "") -> str:
    """Build a three-part agent-oriented error message.

    Args:
        what: the failure, stated plainly (e.g. "absolute paths not allowed").
        why: the reason or rule behind the failure (e.g. "filesystem tools
            sandbox to PRAXIS_WORK_DIR for safety"). Empty string skips it.
        fix: the concrete action the agent should take to correct it (e.g.
            "use a path relative to the work directory"). Empty string skips
            it.

    Returns:
        A single-line message in the form
        ``<what> -- <why> -- <fix>`` (omitting empty parts). Designed to
        be read by the agent in a tool_result or denied event, and by the
        operator in a log.
    """
    parts = [what]
    if why:
        parts.append(why)
    if fix:
        parts.append(fix)
    return " -- ".join(parts)