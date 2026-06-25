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
from .router import ModelRouter, classify_sensitivity
from .multimodal import MediaClient
from .grounding import (GroundedResponder, GroundedAnswer, GroundedPlanner,
                        VerificationResult, generate_json)
from .skills import Skill, SkillLibrary, distill_skill
from .compliance import ComplianceReporter, ComplianceReport, ComplianceFinding
from .task_manager import TaskManager, TaskState
from .wiki import KBSource, KBSourceManager
from .skill_evaluator import SkillEvaluator
from .orchestrator import (AgentPool, AgentSpec, AgentSpecializer,
                           Orchestrator, PredictiveRouter, SubagentRun)
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
    "ModelRouter", "classify_sensitivity",
    "MediaClient",
    "GroundedResponder", "GroundedAnswer", "GroundedPlanner",
    "VerificationResult", "generate_json",
    "Skill", "SkillLibrary", "distill_skill",
    "ComplianceReporter", "ComplianceReport", "ComplianceFinding",
    "TaskManager", "TaskState",
    "KBSource", "KBSourceManager",
    "SkillEvaluator",
    "AgentPool", "AgentSpec", "AgentSpecializer", "Orchestrator",
    "PredictiveRouter", "SubagentRun",
]

__version__ = "0.10.0"
