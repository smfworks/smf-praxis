"""Typed state vocabulary for durable professional workflow runs."""
from __future__ import annotations

from dataclasses import dataclass

RUN_STATUSES = frozenset({
    "running", "interrupted", "cancelled", "completed", "failed",
})
INTERRUPT_TYPES = frozenset({
    "professional_review", "approval", "operator_input", "external_event",
})


@dataclass(frozen=True)
class RunState:
    status: str
    interrupt_type: str = ""

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"unknown run status: {self.status}")
        if self.interrupt_type and self.interrupt_type not in INTERRUPT_TYPES:
            raise ValueError(f"unknown interrupt type: {self.interrupt_type}")
        if self.status == "interrupted" and not self.interrupt_type:
            raise ValueError("interrupted runs require an interrupt type")
        if self.status != "interrupted" and self.interrupt_type:
            raise ValueError("interrupt type is valid only for interrupted runs")
