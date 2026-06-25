"""Persistent task queue for long-running or resumable Praxis work.

This is the first slice of the long-running runtime: tasks are durable rows in
``~/.praxis/praxis.db`` with status, attempts, retry timing, last cycle id, and
result metadata. The executor is deliberately synchronous for now so it can wrap
the existing ``PraxisAgent.handle`` loop without changing public APIs; future
phases can run the same queue from an async worker or background scheduler.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass


TERMINAL = {"completed", "failed", "cancelled"}
RUNNABLE = {"pending", "retry"}


@dataclass
class TaskState:
    task_id: str
    goal: str
    status: str
    attempts: int = 0
    max_attempts: int = 3
    cycle_id: str = ""
    error: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "TaskState":
        return cls(
            task_id=row["task_id"], goal=row["goal"], status=row["status"],
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            cycle_id=row.get("cycle_id", ""), error=row.get("error", ""),
        )


class TaskManager:
    def __init__(self, store) -> None:
        self.store = store

    def create(self, goal: str, max_attempts: int = 3) -> TaskState:
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        self.store.add_task(task_id, goal, max_attempts=max_attempts)
        self.store.add_compliance_event("", "task_created", {
            "task_id": task_id, "goal": goal, "max_attempts": max_attempts,
        }, ref_id=task_id)
        return self.get(task_id)

    def get(self, task_id: str) -> TaskState | None:
        row = self.store.get_task(task_id)
        return TaskState.from_row(row) if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[TaskState]:
        return [TaskState.from_row(r) for r in self.store.list_tasks(status, limit)]

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status in TERMINAL:
            return False
        ok = self.store.update_task(task_id, status="cancelled")
        if ok:
            self.store.add_compliance_event(task.cycle_id, "task_cancelled", {
                "task_id": task_id,
            }, ref_id=task_id)
        return ok

    def run_once(self, task_id: str, agent) -> TaskState:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in TERMINAL:
            return task
        now = time.time()
        row = self.store.get_task(task_id)
        if row.get("next_retry_ts") and row["next_retry_ts"] > now:
            return task

        attempts = task.attempts + 1
        self.store.update_task(task_id, status="running", attempts=attempts)
        self.store.add_compliance_event(task.cycle_id, "task_started", {
            "task_id": task_id, "attempt": attempts,
        }, ref_id=task_id)
        try:
            report = agent.handle(task.goal)
            status = "waiting_approval" if report.pending_approvals else "completed"
            result = {
                "cycle_id": report.cycle_id,
                "summary": report.summary(),
                "actions": report.actions,
                "pending_approvals": report.pending_approvals,
            }
            self.store.update_task(
                task_id, status=status, cycle_id=report.cycle_id,
                result_json=json.dumps(result), error="")
            self.store.add_compliance_event(report.cycle_id, "task_finished", {
                "task_id": task_id, "status": status,
                "pending_approvals": len(report.pending_approvals),
            }, ref_id=task_id)
        except Exception as exc:
            failed_terminal = attempts >= task.max_attempts
            status = "failed" if failed_terminal else "retry"
            next_retry = None if failed_terminal else now + min(3600, 2 ** attempts)
            self.store.update_task(
                task_id, status=status, next_retry_ts=next_retry, error=str(exc))
            self.store.add_compliance_event(task.cycle_id, "task_error", {
                "task_id": task_id, "attempt": attempts,
                "status": status, "error": str(exc),
            }, ref_id=task_id)
        return self.get(task_id)
