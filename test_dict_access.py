#!/usr/bin/env python3
"""Test unguarded dict access in providers._chat_openai."""

# Simulate what happens if API returns malformed response
test_cases = [
    ("empty dict", {}),
    ("no choices", {"model": "gpt-4"}),
    ("empty choices", {"choices": []}),
    ("no message", {"choices": [{"index": 0}]}),
    ("no content", {"choices": [{"message": {"role": "assistant"}}]}),
]

for name, data in test_cases:
    print(f"\nTest: {name}")
    print(f"  Data: {data}")
    try:
        # This is what line 144 does:
        result = data["choices"][0]["message"]["content"]
        print(f"  Result: {result}")
    except (KeyError, IndexError, TypeError) as e:
        print(f"  CRASH: {type(e).__name__}: {e}")
