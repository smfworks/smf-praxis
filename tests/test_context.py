"""Tests for long-context compaction."""

from hybridagent.context import (
    compact_messages,
    compact_tool_messages,
    total_chars,
)


def _tool_history(rounds, *, result_chars=300):
    msgs = [{"role": "user", "content": "do a big job"}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "name": f"tool{i}", "args": {}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "name": f"tool{i}", "content": "x" * result_chars})
    return msgs


def _pairing_ok(messages):
    call_ids = sorted(tc["id"] for m in messages if m.get("role") == "assistant"
                      for tc in (m.get("tool_calls") or []))
    result_ids = sorted(m["tool_call_id"] for m in messages
                        if m.get("role") == "tool")
    return call_ids == result_ids


def test_tool_compact_noop_under_budget():
    msgs = _tool_history(2)
    assert compact_tool_messages(msgs, max_chars=100000) == msgs
    assert compact_tool_messages(msgs, max_chars=0) == msgs


def test_tool_compact_preserves_pairing_and_shrinks():
    msgs = _tool_history(12)
    out = compact_tool_messages(msgs, max_chars=1500, keep_recent=2)
    assert _pairing_ok(out)
    assert total_chars(out) < total_chars(msgs)
    assert msgs[0] in out                # user goal kept
    assert msgs[-1] in out and msgs[-2] in out  # last round kept verbatim
    assert any("compacted to save context" in str(m.get("content", "")) for m in out)


def test_tool_compact_recap_has_no_tool_calls():
    out = compact_tool_messages(_tool_history(10), max_chars=1200, keep_recent=2)
    recap = next(m for m in out if "compacted to save context" in str(m.get("content", "")))
    assert "tool_calls" not in recap  # the synthetic recap can't orphan a result


def test_tool_compact_custom_summarizer():
    out = compact_tool_messages(_tool_history(10), max_chars=1200,
                                summarize=lambda t: "RECAP")
    assert any(m.get("content") == "RECAP" for m in out)
    assert _pairing_ok(out)


def test_compact_noop_under_budget():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}]
    assert compact_messages(msgs, max_chars=1000, keep_recent=8) == msgs


def test_compact_summarizes_older_keeps_recent():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant",
             "content": "x" * 500} for i in range(20)]
    out = compact_messages(msgs, max_chars=1000, keep_recent=4,
                           summarize=lambda t: "SUMMARY")
    assert out[0]["role"] == "system" and "SUMMARY" in out[0]["content"]
    assert out[-4:] == msgs[-4:]
    assert len(out) == 5
    assert total_chars(out) < total_chars(msgs)


def test_compact_preserves_leading_system():
    sysmsg = {"role": "system", "content": "persona"}
    msgs = [sysmsg] + [{"role": "user", "content": "y" * 500} for _ in range(10)]
    out = compact_messages(msgs, max_chars=800, keep_recent=3,
                           summarize=lambda t: "S")
    assert out[0] == sysmsg
    assert out[1]["role"] == "system" and "S" in out[1]["content"]
    assert out[-3:] == msgs[-3:]


def test_compact_disabled_when_budget_zero():
    msgs = [{"role": "user", "content": "x" * 5000} for _ in range(10)]
    assert compact_messages(msgs, max_chars=0, keep_recent=2) == msgs


def test_compact_keep_recent_zero_summarizes_all():
    msgs = [{"role": "user", "content": "x" * 500} for _ in range(8)]
    out = compact_messages(msgs, max_chars=800, keep_recent=0,
                           summarize=lambda t: "S")
    # keep_recent=0 means "summarize everything" -> just the summary note.
    assert len(out) == 1 and out[0]["role"] == "system" and "S" in out[0]["content"]
    assert total_chars(out) < total_chars(msgs)
