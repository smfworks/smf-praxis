"""First-class SMF vertical jobs — research, draft-behind-approval, scheduled colleague.

These are thin, governed entry points over existing Praxis surfaces (research API,
governed chat / task queue, cron). They exist so the product story is three clear
jobs rather than a generic agent console.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobSpec:
    id: str
    title: str
    summary: str
    how: str
    example_prompt: str
    mode: str  # research | chat | do | cron
    risk_note: str


JOBS: list[JobSpec] = [
    JobSpec(
        id="research",
        title="Research → brief",
        summary="Search the web, read top sources, return a cited brief.",
        how="Uses live web research (DuckDuckGo by default) + grounded synthesis.",
        example_prompt="Research the latest open-source agent runtimes and summarize in 5 bullets with sources.",
        mode="research",
        risk_note="Read-only. No sends.",
    ),
    JobSpec(
        id="draft",
        title="Draft behind approval",
        summary="Write a professional draft; sending stays held for your approval.",
        how="Governed chat/tools — drafts run autonomously; send/destructive are held.",
        example_prompt=(
            "Draft a short follow-up email to Alex thanking them for the meeting "
            "and proposing next Tuesday. Do not send it."
        ),
        mode="chat",
        risk_note="Send is held for approval (A once · C this chat · D deny).",
    ),
    JobSpec(
        id="schedule",
        title="Scheduled colleague",
        summary="Recurring autonomous work: scan → draft → hold consequential sends.",
        how="Cron job on the daemon tick; same governance as interactive Do tasks.",
        example_prompt="Every weekday at 9:00, scan for urgent follow-ups and draft a short status note.",
        mode="cron",
        risk_note="Scheduled goals still pass the broker; sends need approval.",
    ),
]


def list_jobs() -> list[dict[str, Any]]:
    return [
        {
            "id": j.id,
            "title": j.title,
            "summary": j.summary,
            "how": j.how,
            "example_prompt": j.example_prompt,
            "mode": j.mode,
            "risk_note": j.risk_note,
        }
        for j in JOBS
    ]


def get_job(job_id: str) -> JobSpec | None:
    for j in JOBS:
        if j.id == job_id:
            return j
    return None


def run_research(daemon, query: str, max_results: int = 5) -> dict:
    """Execute the research job via the daemon."""
    return daemon.research(query, max_results=max_results)


def run_draft(daemon, prompt: str) -> Any:
    """Execute draft-behind-approval via governed chat agent (iterator of events)."""
    messages = [{"role": "user", "content": prompt}]
    return daemon.chat_agent(messages)


def schedule_colleague(
    store,
    goal: str,
    schedule: str = "0 9 * * 1-5",
    name: str = "colleague",
) -> dict:
    """Create a cron job for the scheduled-colleague vertical."""
    from .cron import CronScheduler

    sched = CronScheduler(store)
    job = sched.create(goal, schedule, name=name, mode="do")
    return job
