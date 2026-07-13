"""Praxis — an autonomous AI colleague.

Proactive, local-first action plus persistent multi-tier memory, editorial
judgment, and self-improvement — fused behind a governance broker so the result
is proactive AND safe.

Loop:  perceive -> plan -> govern -> act/draft -> reflect -> consolidate
"""
from . import ingest
from .agent import PraxisAgent
from .agent_service import AgentService
from .bm25 import BM25Index
from .broker import Decision, GovernanceBroker, GovernancePolicy, KillSwitch, RiskClass
from .compliance import ComplianceFinding, ComplianceReport, ComplianceReporter
from .content_guard import GuardedContent, guard_tool_result
from .contradiction import Contradiction
from .contradiction import detect as detect_contradictions
from .debate import Candidate, DebatePanel, DebateResult
from .deepthink import DeepThink, DeepThinkResult
from .embeddings import EmbeddingClient
from .eval_history import RegressionReport, compare_reports
from .grounding import (
                        GroundedAnswer,
                        GroundedPlanner,
                        GroundedResponder,
                        VerificationResult,
                        generate_json,
)
from .llm import LLMClient
from .mcp_client import (
    MCPClient,
    StdioTransport,
    augment_registry_with_mcp,
    mcp_tools,
    risk_for_tool,
)
from .memory import Memory, MemoryItem, Tier
from .metrics import HealthMonitor, HealthSnapshot
from .multimodal import MediaClient
from .orchestrator import (
                        AgentPool,
                        AgentSpec,
                        AgentSpecializer,
                        Orchestrator,
                        PredictiveRouter,
                        SubagentRun,
)
from .perception import Perception, Signal
from .persistence import Store
from .plan_execute import ExecutionReport, PlanExecutor, PlanStep, to_plan_steps
from .planner import Plan, Planner, Step
from .rag import Rag, RetrievedChunk, chunk_text, reciprocal_rank_fusion
from .reflection import Reflector
from .reflexion import ReflexionConfig, ReflexiveChatAgent
from .router import ModelRouter, classify_sensitivity
from .router_model import RouterModel
from .scratchpad import Scratchpad, ScratchpadEntry
from .skill_evaluator import SkillEvaluator
from .skills import Skill, SkillLibrary, distill_skill
from .task_manager import TaskManager, TaskState
from .tools import Tool, ToolRegistry, default_registry
from .validation import ValidationError, validate, validate_tool_args
from .verifier import AnswerVerifier, VerificationConfig, VerifiedChatAgent
from .wiki import KBSource, KBSourceManager
from .wiki_safe import UnsafeSourceError, fetch_url, validate_uri

__all__ = [
    "PraxisAgent",
    "Memory", "MemoryItem", "Tier",
    "GovernanceBroker", "GovernancePolicy", "Decision", "RiskClass", "KillSwitch",
    "Tool", "ToolRegistry", "default_registry",
    "Planner", "Step", "Plan",
    "PlanExecutor", "PlanStep", "ExecutionReport", "to_plan_steps",
    "AgentService",
    "Perception", "Signal",
    "Reflector",
    "LLMClient",
    "Store",
    "EmbeddingClient",
    "Rag", "RetrievedChunk", "chunk_text", "ingest", "reciprocal_rank_fusion",
    "ModelRouter", "classify_sensitivity",
    "RouterModel",
    "ReflexiveChatAgent", "ReflexionConfig",
    "AnswerVerifier", "VerifiedChatAgent", "VerificationConfig",
    "DebatePanel", "DebateResult", "Candidate",
    "DeepThink", "DeepThinkResult",
    "MCPClient", "StdioTransport", "mcp_tools", "risk_for_tool",
    "augment_registry_with_mcp",
    "RegressionReport", "compare_reports",
    "GuardedContent", "guard_tool_result",
    "BM25Index",
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
    "ValidationError", "validate", "validate_tool_args",
    "Contradiction", "detect_contradictions",
    "Scratchpad", "ScratchpadEntry",
    "HealthMonitor", "HealthSnapshot",
    "UnsafeSourceError", "fetch_url", "validate_uri",
]

__version__ = "0.26.2"
