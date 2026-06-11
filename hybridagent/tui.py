"""Praxis TUI — a stdlib, dependency-free interactive terminal UI.

Launch with ``praxis tui``. Keeps one agent for the whole session so memory,
learned skills, and pending approvals persist between actions.
"""
from __future__ import annotations

import os
import sys

from . import PraxisAgent
from . import config as cfg
from . import onboard as onboard_mod


def _color(s: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def _bold(s: str) -> str: return _color(s, "1")
def _cyan(s: str) -> str: return _color(s, "36")
def _green(s: str) -> str: return _color(s, "32")
def _yellow(s: str) -> str: return _color(s, "33")
def _dim(s: str) -> str: return _color(s, "2")


def _clear() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _provider_line() -> str:
    model = cfg.get_default_model()
    if model:
        return _green(f"provider: {model}")
    return _yellow("provider: not configured (OFFLINE MOCK mode)")


MENU = [
    ("Run a goal (perceive -> govern -> act -> reflect)", "handle"),
    ("Heartbeat (proactive scan tick)", "heartbeat"),
    ("Review pending approvals", "approvals"),
    ("View memory (tiers, skills, durable facts)", "memory"),
    ("View audit trail", "audit"),
    ("Configure model provider (onboard)", "onboard"),
    ("Quit", "quit"),
]


def render_menu() -> str:
    lines = [_bold("Praxis — hybrid autonomous colleague"), _provider_line(), ""]
    for i, (label, _key) in enumerate(MENU, 1):
        lines.append(f"  {_cyan(str(i))}. {label}")
    return "\n".join(lines)


def _print_report(agent: PraxisAgent, report) -> None:
    print(_bold(f"\nGoal: {report.goal}"))
    print(_bold("Actions:"))
    for a in report.actions:
        tag = a.split("]", 1)[0] + "]" if a.startswith("[") else ""
        rest = a[len(tag):]
        color = _green if "->" in a and "HELD" not in a and "DENIED" not in a else _yellow
        print("  " + color(tag) + rest)
    if report.injection_flags:
        print(_yellow("Injection-flagged (treated as data): ") + ", ".join(report.injection_flags))
    if report.pending_approvals:
        print(_yellow("\nPending approvals (held for you):"))
        for ap in report.pending_approvals:
            print(f"   {_bold(ap['approval_id'])} [{ap['risk']}] {ap['tool']}")
    if report.reflection and report.reflection.skill:
        print(_dim(f"\nlearned skill: {report.reflection.skill}"))
    print(_dim(f"memory: {agent.memory.stats()}"))


def _pause() -> None:
    input(_dim("\n[enter] to continue "))


def _do_handle(agent: PraxisAgent) -> None:
    goal = input("\nEnter goal: ").strip()
    if not goal:
        return
    report = agent.handle(goal)
    _print_report(agent, report)
    if report.pending_approvals:
        ans = input(_yellow("\nApprove held action(s)? [a]ll / [n]one / id: ")).strip().lower()
        if ans == "a":
            for ap in list(report.pending_approvals):
                print("  ", _green(agent.approve(ap["approval_id"])))
        elif ans and ans not in ("n", "none"):
            print("  ", _green(agent.approve(ans)))
    _pause()


def _do_heartbeat(agent: PraxisAgent) -> None:
    watch = input("\nWatch goal [scan for urgent follow-ups]: ").strip() or "scan for urgent follow-ups"
    _print_report(agent, agent.heartbeat(watch))
    _pause()


def _do_approvals(agent: PraxisAgent) -> None:
    pend = agent.broker.pending
    if not pend:
        print(_dim("\nNo pending approvals."))
    else:
        print(_yellow("\nPending approvals:"))
        for aid, p in pend.items():
            print(f"   {_bold(aid)} [{p.tool}] {p.preview}")
        ans = input("\nApprove which id (or [a]ll / blank to skip): ").strip().lower()
        if ans == "a":
            for aid in list(pend):
                print("  ", _green(agent.approve(aid)))
        elif ans:
            print("  ", _green(agent.approve(ans)))
    _pause()


def _do_memory(agent: PraxisAgent) -> None:
    print(_bold("\nMemory tiers: ") + str(agent.memory.stats()))
    durable = agent.memory.durable
    if durable:
        print(_bold("Durable:"))
        for it in durable:
            print(f"  [{it.kind}] {it.text}  {_dim('(' + it.provenance + ')')}")
    _pause()


def _do_audit(agent: PraxisAgent) -> None:
    print(_bold(f"\nAudit trail ({len(agent.broker.audit)} entries):"))
    for e in agent.broker.audit[-20:]:
        v = _green(e.verdict) if e.verdict == "allow" else _yellow(e.verdict)
        print(f"  {e.actor} {e.tool} [{e.risk}] -> {v}  {_dim(e.detail)}")
    _pause()


def run() -> int:
    agent = PraxisAgent()
    actions = {
        "handle": _do_handle, "heartbeat": _do_heartbeat, "approvals": _do_approvals,
        "memory": _do_memory, "audit": _do_audit,
    }
    while True:
        _clear()
        print(render_menu())
        choice = input("\nSelect [1-{0}]: ".format(len(MENU))).strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
            continue
        key = MENU[int(choice) - 1][1]
        if key == "quit":
            print(_dim("Goodbye."))
            return 0
        if key == "onboard":
            onboard_mod.run()
            _pause()
            continue
        actions[key](agent)


if __name__ == "__main__":
    sys.exit(run())
