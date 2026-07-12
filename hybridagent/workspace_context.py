"""Canonical workspace scope for professional context and persistence."""
from __future__ import annotations

from dataclasses import dataclass

from .persistence import Store
from .workspaces import WorkspaceDirectory


@dataclass(frozen=True)
class WorkspaceScope:
    store: Store
    organization_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        if WorkspaceDirectory(self.store).get(
                self.organization_id, self.workspace_id) is None:
            raise ValueError("workspace does not exist in organization")

    @property
    def knowledge_namespace(self) -> str:
        return self.context_key("knowledge", "primary")

    def context_key(self, kind: str, local_id: str) -> str:
        clean_kind = kind.strip()
        clean_id = local_id.strip()
        if not clean_kind or not clean_id or ":" in clean_kind:
            raise ValueError("context kind and local identifier are required")
        return (f"workspace:{self.organization_id}:{self.workspace_id}:"
                f"{clean_kind}:{clean_id}")

    def add_memory(self, tier: str, text: str, provenance: str,
                   kind: str) -> int:
        return self.store.add_memory(
            tier, text, provenance, kind, workspace_id=self.workspace_id)

    def load_memory(self, tier: str) -> list[dict]:
        return self.store.load_memory(tier, workspace_id=self.workspace_id)

    def start_run(self, run_id: str, goal: str = "", kind: str = "plan") -> None:
        self.store.start_run(
            run_id, goal, kind, workspace_id=self.workspace_id)

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self.store.list_runs(limit, workspace_id=self.workspace_id)

    def get_run(self, run_id: str) -> dict | None:
        return self.store.get_run(run_id, workspace_id=self.workspace_id)

    def run_events(self, run_id: str) -> list[dict]:
        if self.get_run(run_id) is None:
            return []
        return self.store.list_run_events(
            run_id, workspace_id=self.workspace_id)

    def add_card(self, card_id: str, title: str, goal: str, *,
                 run_id: str = "") -> None:
        if run_id and self.get_run(run_id) is None:
            raise ValueError("run does not belong to workspace")
        self.store.add_card(
            card_id, title, goal, organization_id=self.organization_id,
            workspace_id=self.workspace_id, run_id=run_id)

    def list_cards(self, limit: int = 200) -> list[dict]:
        return self.store.list_cards(
            limit, organization_id=self.organization_id,
            workspace_id=self.workspace_id)

    def get_card(self, card_id: str) -> dict | None:
        return self.store.get_card(card_id, workspace_id=self.workspace_id)
