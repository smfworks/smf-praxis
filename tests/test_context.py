"""Tests for long-context compaction."""

from hybridagent.context import compact_messages, total_chars


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
