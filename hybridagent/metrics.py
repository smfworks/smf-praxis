"""Runtime metrics for monitoring + health checks.

Pulls aggregate counts and risk indicators out of the persistent store so an
operator can answer "is Praxis healthy?" without parsing the SQLite tables by
hand. Mirrors what a future ``/metrics`` HTTP endpoint would return.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass


@dataclass
class HealthSnapshot:
    cycles: int
    audit_entries: int
    pending_approvals: int
    failed_tasks: int
    waiting_approval_tasks: int
    errored_kb_sources: int
    stale_kb_sources: int
    quarantined_skills: int
    stale_agents: int
    last_compliance_event_age_seconds: float | None
    healthy: bool

    def as_dict(self) -> dict:
        return asdict(self)


class HealthMonitor:
    def __init__(self, store) -> None:
        self.store = store

    def snapshot(self, *, stale_kb_threshold_seconds: float = 7 * 86400,
                 stale_agent_threshold_seconds: float = 3600) -> HealthSnapshot:
        now = time.time()
        events = self.store.list_compliance_events(limit=1)
        last_age = (now - events[0]["ts"]) if events else None

        approvals = self.store.list_approvals()
        kb_sources = self.store.list_kb_sources()
        tasks = self.store.list_tasks()
        skill_meta = self.store.list_skill_metadata()
        agents = self.store.list_agent_instances()

        errored_kb = sum(1 for s in kb_sources if s.get("status") == "error")
        stale_kb = sum(
            1 for s in kb_sources
            if s.get("last_ingested_ts") is not None
            and (now - s["last_ingested_ts"]) > stale_kb_threshold_seconds
        )
        failed_tasks = sum(1 for t in tasks if t.get("status") == "failed")
        waiting_tasks = sum(
            1 for t in tasks if t.get("status") == "waiting_approval"
        )
        quarantined = sum(1 for m in skill_meta if m.get("quarantined"))
        stale_agents = sum(
            1 for a in agents
            if a.get("last_heartbeat_ts") is not None
            and (now - a["last_heartbeat_ts"]) > stale_agent_threshold_seconds
        )
        cycles = len({e["cycle_id"]
                      for e in self.store.list_compliance_events(limit=10000)
                      if e.get("cycle_id")})

        healthy = (
            errored_kb == 0 and failed_tasks == 0
            and stale_agents == 0
        )
        return HealthSnapshot(
            cycles=cycles,
            audit_entries=len(self.store.load_audit(limit=10000)),
            pending_approvals=len(approvals),
            failed_tasks=failed_tasks,
            waiting_approval_tasks=waiting_tasks,
            errored_kb_sources=errored_kb,
            stale_kb_sources=stale_kb,
            quarantined_skills=quarantined,
            stale_agents=stale_agents,
            last_compliance_event_age_seconds=last_age,
            healthy=healthy,
        )

    @staticmethod
    def render(snap: HealthSnapshot) -> str:
        status = "HEALTHY" if snap.healthy else "DEGRADED"
        lines = [f"status: {status}"]
        for k, v in snap.as_dict().items():
            if k == "healthy":
                continue
            lines.append(f"{k}: {v}")
        return "\n".join(lines)
