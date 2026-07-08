"""Intent router for friendly Auto mode (Friendliness Sprint A)."""

from hybridagent.intent import detect_intent, detect_intent_auto


def test_empty_defaults_to_chat():
    assert detect_intent_auto("") == "chat"
    assert detect_intent_auto("   ") == "chat"


def test_research_url_and_keywords():
    assert detect_intent_auto("Summarize https://example.com for me") == "research"
    assert detect_intent_auto("Please research the latest news on agent safety") == "research"
    assert detect_intent_auto("look up competing open source agents") == "research"


def test_ask_grounded():
    assert detect_intent_auto(
        "According to my knowledge base, what is Praxis?"
    ) == "ask"
    assert detect_intent_auto("cite your sources from the wiki") == "ask"


def test_do_background():
    assert detect_intent_auto("Queue a task: draft a status note") == "do"
    assert detect_intent_auto("work on this in the background") == "do"
    assert detect_intent_auto("add to the board: follow up with vendors") == "do"


def test_agent_tools():
    assert detect_intent_auto("use tools to browse the docs site") == "agent"
    assert detect_intent_auto("open the browser and click login") == "agent"


def test_default_chat():
    assert detect_intent_auto("Hey, how are you today?") == "chat"
    assert detect_intent_auto("Explain the governance broker briefly") == "chat"


def test_explicit_mode_wins():
    assert detect_intent("Summarize https://x.com", explicit="ask") == "ask"
    assert detect_intent("queue a task", explicit="chat") == "chat"
    assert detect_intent("hello", explicit=None) == "chat"


def test_priority_do_before_ask():
    # Do patterns are checked first so background handoff wins over look-up phrasing.
    assert detect_intent_auto(
        "Queue a task: according to my knowledge base, summarize open items"
    ) == "do"
