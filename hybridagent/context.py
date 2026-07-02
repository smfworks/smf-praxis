"""Long-context management — compact an over-budget chat history.

Keeps the most recent turns verbatim and replaces older ones with a single
summary note, so long conversations still fit a model's context window. Pure and
deterministic given a deterministic summarizer (the offline mock qualifies).

Applied to the conversational surfaces (chat / streaming chat) via
:func:`compact_messages`. The governed tool loop uses the pairing-aware
:func:`compact_tool_messages` instead, which keeps each assistant ``tool_calls``
together with its ``tool`` results.
"""
from __future__ import annotations

from collections.abc import Callable


def _content_len(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    return len(str(content)) if content is not None else 0


def total_chars(messages: list[dict]) -> int:
    return sum(_content_len(m) for m in messages or [])


def compact_messages(messages: list[dict], *, max_chars: int = 24000,
                     keep_recent: int = 8,
                     summarize: Callable[[str], str] | None = None) -> list[dict]:
    """Return a history that fits ``max_chars`` by summarizing older turns.

    Leading system turns are preserved; the oldest non-recent turns are folded
    into one summary note inserted ahead of the ``keep_recent`` most recent
    turns. Returns the input unchanged when it is already within budget (or when
    ``max_chars <= 0``, which disables compaction).
    """
    msgs = list(messages or [])
    if max_chars <= 0 or total_chars(msgs) <= max_chars:
        return msgs
    keep_recent = max(0, keep_recent)
    if keep_recent and len(msgs) <= keep_recent:
        return msgs

    lead_system: list[dict] = []
    body = msgs
    while body and body[0].get("role") == "system":
        lead_system.append(body[0])
        body = body[1:]
    if keep_recent and len(body) <= keep_recent:
        return msgs

    if keep_recent:
        older = body[:-keep_recent]
        recent = body[-keep_recent:]
    else:  # keep_recent == 0 -> summarize everything
        older = body
        recent = []
    transcript = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in older)
    if summarize is not None:
        try:
            summary = summarize(transcript)
        except Exception:
            summary = transcript[: max(1, max_chars // 4)]
    else:
        summary = transcript[: max(1, max_chars // 4)]

    note = {
        "role": "system",
        "content": ("Summary of earlier conversation (older turns were compacted "
                    "to fit the context window):\n" + str(summary)),
    }
    return lead_system + [note] + recent


def _tool_blocks(messages: list[dict]) -> list[list[dict]]:
    """Group a tool-loop history into atomic blocks.

    A block is either a single non-tool message, or an assistant ``tool_calls``
    message together with the contiguous ``tool`` results that answer it — so a
    call is never separated from its result.
    """
    blocks: list[list[dict]] = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            group = [m]
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                group.append(messages[j])
                j += 1
            blocks.append(group)
            i = j
        else:
            blocks.append([m])
            i += 1
    return blocks


def _recap_for(blocks: list[list[dict]],
               summarize: Callable[[str], str] | None) -> str:
    if summarize is not None:
        transcript = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}"
                               for g in blocks for m in g)
        try:
            return str(summarize(transcript))
        except Exception:
            pass
    names: list[str] = []
    for group in blocks:
        head = group[0]
        if head.get("role") == "assistant":
            names += [tc.get("name", "?") for tc in head.get("tool_calls") or []]
    uniq = list(dict.fromkeys(n for n in names if n))
    detail = ("ran tools " + ", ".join(uniq) + "; ") if uniq else ""
    return ("[Earlier steps in this turn were compacted to save context: "
            + detail + "continue from the recent steps below.]")


def compact_tool_messages(messages: list[dict], *, max_chars: int = 24000,
                          keep_recent: int = 2,
                          summarize: Callable[[str], str] | None = None,
                          ) -> list[dict]:
    """Compact an over-budget governed-tool-loop history, keeping pairing intact.

    Unlike :func:`compact_messages`, this operates on whole tool-call *blocks*
    (an assistant tool request plus its ``tool`` results), so an assistant
    ``tool_calls`` message is never split from the results that answer it. The
    first block (the user's goal) and the last ``keep_recent`` blocks are kept
    verbatim; the middle blocks are folded into a single, tool-call-free recap.
    If a small number of blocks still exceeds the budget (e.g. one huge tool
    result), individual tool-result contents are truncated to fit.
    Returns the input unchanged when already within budget or ``max_chars <= 0``.
    """
    msgs = list(messages or [])
    if max_chars <= 0 or total_chars(msgs) <= max_chars:
        return msgs
    keep_recent = max(1, keep_recent)
    blocks = _tool_blocks(msgs)
    if len(blocks) > keep_recent + 1:
        lead, recent = blocks[0], blocks[-keep_recent:]
        middle = blocks[1:-keep_recent]
        recap = {"role": "assistant", "content": _recap_for(middle, summarize)}
        out: list[dict] = [*lead, recap]
        for group in recent:
            out.extend(group)
        if total_chars(out) <= max_chars:
            return out

    # Still over budget: truncate oversized tool results so the model call
    # does not blow up the context window and fall back to mock/offline mode.
    # Preserve the most recent tool result fully if possible; cap older ones.
    budget = max_chars
    out = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        if role == "tool" and isinstance(content, str) and len(content) > budget // 2:
            truncated = content[:budget // 2]
            content = (
                truncated
                + "\n\n[Tool result truncated by Praxis from "
                f"{len(content)} to {len(truncated)} chars to fit the context window.]"
            )
            m = {**m, "content": content}
        out.append(m)
    return out
