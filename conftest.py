import os
import sys

# Tests always use the offline mock LLM, regardless of any machine-level config.
os.environ.setdefault("PRAXIS_LLM", "mock")

sys.path.insert(0, os.path.dirname(__file__))
