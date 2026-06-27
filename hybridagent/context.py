"""Long-context management — compact an over-budget chat history.

Keeps the most recent turns verbatim and replaces older ones with a single
summary note, so long conversations still fit a model's context window. Pure and
deterministic given a deterministic summarizer (the offline mock qualifies).

Applied to the conversational surfaces (chat / streaming chat). It is deliberately
*not* applied to the governed tool loop, where assistant ``tool_calls`` and their
``tool`` results must stay paired.
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
    if max_chars <= 0 or len(msgs) <= keep_recent or total_chars(msgs) <= max_chars:
        return msgs

    lead_system: list[dict] = []
    body = msgs
    while body and body[0].get("role") == "system":
        lead_system.append(body[0])
        body = body[1:]
    if len(body) <= keep_recent:
        return msgs

    older = body[:-keep_recent]
    recent = body[-keep_recent:]
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
