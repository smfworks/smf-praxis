"""Praxis hybrid-agent demo (offline, mock LLM).

    python demo.py

Thin wrapper around :mod:`hybridagent.demo` so the demo runs from a repo
checkout. The real demo lives inside the package, so ``praxis demo`` works for
installed wheels too. Shows the full loop, autonomy vs. approval gating,
prompt-injection handling, and self-improving memory consolidation.
"""
from __future__ import annotations

from hybridagent.demo import main

if __name__ == "__main__":
    main()
