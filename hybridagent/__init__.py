"""Praxis — a hybrid autonomous AI colleague.

Combines OpenClaw's proactive, local-first action ecosystem with Hermes'
persistent multi-tier memory, editorial judgment, and self-improvement — fused
behind a governance broker so the result is proactive AND safe.

Loop:  perceive -> plan -> govern -> act/draft -> reflect -> consolidate
"""
from .agent import PraxisAgent
from .memory import Memory, MemoryItem, Tier
from .broker import GovernanceBroker, GovernancePolicy, Decision, RiskClass, KillSwitch
from .tools import Tool, ToolRegistry, default_registry
from .planner import Planner, Step, Plan
from .perception import Perception, Signal
from .reflection import Reflector
from .llm import LLMClient
from .persistence import Store
from .embeddings import EmbeddingClient
from .rag import Rag, RetrievedChunk, chunk_text
from . import ingest

__all__ = [
    "PraxisAgent",
    "Memory", "MemoryItem", "Tier",
    "GovernanceBroker", "GovernancePolicy", "Decision", "RiskClass", "KillSwitch",
    "Tool", "ToolRegistry", "default_registry",
    "Planner", "Step", "Plan",
    "Perception", "Signal",
    "Reflector",
    "LLMClient",
    "Store",
    "EmbeddingClient",
    "Rag", "RetrievedChunk", "chunk_text", "ingest",
]

__version__ = "0.2.0"
