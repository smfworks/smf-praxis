"""Command-line interface for Praxis (hybrid autonomous colleague).

Usage:
    praxis demo                              # run the bundled demo
    praxis handle "<goal>"                   # run one perceive->...->consolidate cycle
    praxis handle "<goal>" --approve-all     # auto-approve held actions (dev only)
    praxis heartbeat                         # proactive always-on tick
    praxis remember "<fact>" --kind preference

If installed via `pip install -e .` the entry point is `praxis`; otherwise run
`python -m hybridagent.cli ...`.
"""
from __future__ import annotations

import argparse
import sys

from . import PraxisAgent
from . import config as cfg
from . import onboard as onboard_mod


def _print_report(agent: PraxisAgent, report) -> None:
    print(f"goal: {report.goal}")
    print("actions:")
    for a in report.actions:
        print("  ", a)
    if report.injection_flags:
        print("injection-flagged sources (treated as data):", report.injection_flags)
    if report.pending_approvals:
        print("pending approvals (held for human):")
        for appr in report.pending_approvals:
            print(f"   {appr['approval_id']}  [{appr['risk']}] {appr['tool']} :: {appr['preview']}")
    if report.reflection:
        print("reflection:", report.reflection)
    print("memory:", agent.memory.stats())


def cmd_handle(args: argparse.Namespace) -> int:
    agent = PraxisAgent()
    report = agent.handle(args.goal)
    if args.approve_all:
        for appr in list(report.pending_approvals):
            print("auto-approving", appr["approval_id"], "->",
                  agent.approve(appr["approval_id"]))
    _print_report(agent, report)
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    agent = PraxisAgent()
    report = agent.heartbeat(args.watch)
    _print_report(agent, report)
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    agent = PraxisAgent()
    agent.learn(args.fact, kind=args.kind, provenance="cli")
    print(f"stored durable {args.kind}: {args.fact}")
    print("memory:", agent.memory.stats())
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    if args.provider and args.model:
        summary = onboard_mod.run_noninteractive(
            args.provider, args.model, base_url=args.base_url,
            api_key=args.api_key, use_env_ref=not args.api_key,
        )
        print(f"Configured (non-interactive): model = {summary['model']}")
        print(f"Config written to: {cfg.config_path()}")
        return 0
    onboard_mod.run()
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    import os
    import runpy
    demo = os.path.join(os.path.dirname(os.path.dirname(__file__)), "demo.py")
    runpy.run_path(demo, run_name="__main__")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="praxis", description="Praxis hybrid-agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    ph = sub.add_parser("handle", help="run one full agent cycle for a goal")
    ph.add_argument("goal", help="the goal text")
    ph.add_argument("--approve-all", action="store_true",
                    help="auto-approve held consequential actions (dev only)")
    ph.set_defaults(func=cmd_handle)

    pb = sub.add_parser("heartbeat", help="proactive always-on tick")
    pb.add_argument("--watch", default="scan for urgent follow-ups",
                    help="the watch goal to run")
    pb.set_defaults(func=cmd_heartbeat)

    pm = sub.add_parser("remember", help="store a durable fact/preference")
    pm.add_argument("fact", help="the fact text")
    pm.add_argument("--kind", default="preference",
                    choices=["preference", "fact", "decision", "skill", "note"])
    pm.set_defaults(func=cmd_remember)

    pd = sub.add_parser("demo", help="run the bundled demo")
    pd.set_defaults(func=cmd_demo)

    po = sub.add_parser("onboard", help="pick a model provider + model (interactive)")
    po.add_argument("--provider", choices=["ollama", "openrouter", "github",
                                           "openai", "anthropic", "custom"],
                    help="non-interactive: provider id")
    po.add_argument("--model", help="non-interactive: model id")
    po.add_argument("--base-url", default=None, help="non-interactive: custom base URL")
    po.add_argument("--api-key", default=None,
                    help="non-interactive: paste key (else an env reference is used)")
    po.set_defaults(func=cmd_onboard)
    return parser


def _maybe_first_run_onboard(command: str) -> None:
    """Offer onboarding on first use when nothing is configured (TTY only)."""
    if command in ("onboard", "demo"):
        return
    if cfg.is_configured() or not sys.stdin.isatty():
        return
    print("No model provider configured yet. Praxis will run in OFFLINE MOCK mode.")
    ans = input("Run setup now to pick a provider (Ollama/OpenRouter/GitHub/...)? [y/N]: ").strip().lower()
    if ans == "y":
        onboard_mod.run()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _maybe_first_run_onboard(args.command)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
