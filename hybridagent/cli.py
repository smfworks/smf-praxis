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
import os
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


def _make_agent(args: argparse.Namespace):
    if getattr(args, "m365", False):
        from .m365_tools import build_m365_agent
        from .persistence import Store
        agent, _client = build_m365_agent(store=Store.open())
        return agent
    return PraxisAgent.persistent()


def cmd_handle(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    report = agent.handle(args.goal)
    if args.approve_all:
        for appr in list(report.pending_approvals):
            print("auto-approving", appr["approval_id"], "->",
                  agent.approve(appr["approval_id"]))
    _print_report(agent, report)
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    report = agent.heartbeat(args.watch)
    _print_report(agent, report)
    return 0


def cmd_m365(_args: argparse.Namespace) -> int:
    from .broker_client import BrokerClient
    client = BrokerClient.from_env()
    health = client.health()
    print("broker health:", health)
    if not health.get("ok"):
        print("Broker not reachable. Start it with `npm start` in openclaw-m365-broker,")
        print("and set M365_BROKER_URL / M365_BROKER_KEY (and M365_BROKER_APPROVER_KEY).")
        return 1
    print("least-privilege scopes:", health.get("requiredScopes"))
    print("status:", client.execute("m365_status"))
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    agent = PraxisAgent.persistent()
    agent.learn(args.fact, kind=args.kind, provenance="cli")
    print(f"stored durable {args.kind}: {args.fact}")
    print("memory:", agent.memory.stats())
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    pending = agent.broker.pending
    if not pending:
        print("no pending approvals")
        return 0
    print(f"{len(pending)} pending approval(s):")
    for aid, p in pending.items():
        print(f"   {aid}  [{p.tool}] {p.preview}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    print(agent.approve(args.approval_id))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .rag import Rag
    rag = Rag(Store.open())
    total = 0
    for path in args.paths:
        try:
            doc, n = rag.ingest_file(path)
            print(f"ingested {doc.source} ({doc.kind}): {n} chunks")
            total += n
        except Exception as exc:
            print(f"  skip {path}: {exc}")
    print(f"+{total} chunks. KB now: {rag.stats()}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .rag import Rag
    rag = Rag(Store.open())
    hits = rag.retrieve(args.query, k=args.k)
    if not hits:
        print("no matches (KB empty? run 'praxis ingest <file>')")
        return 0
    for h in hits:
        snippet = " ".join(h.text.split())[:200]
        print(f"[{h.score:.3f}] {h.source} ({h.kind})  {h.provenance}")
        print(f"    {snippet}")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    from .ingest import extract_text
    from .multimodal import MediaClient
    mc = MediaClient()
    try:
        doc = mc.process(args.path) if mc.is_media(args.path) else extract_text(args.path)
    except Exception as exc:
        print(f"could not process {args.path}: {exc}")
        return 1
    print(f"# {doc.source} ({doc.kind})")
    print(doc.text[:2000])
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    from .router import ModelRouter
    r = ModelRouter()
    roles = ["general", "planner", "summarizer", "vision", "transcribe"]
    print(f"{'role':<12} {'sensitivity':<11} candidates (primary first)")
    for role in roles:
        for sens in ("normal", "sensitive"):
            exp = r.explain(role, sens)
            tag = " [local]" if exp["primary_is_local"] else ""
            print(f"{role:<12} {sens:<11} {exp['candidates']}{tag}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    ans = agent.ask(args.question, k=args.k)
    if ans.abstained:
        print("INSUFFICIENT EVIDENCE — Praxis declined to answer rather than guess.")
        print(ans.text)
        return 0
    print(ans.text)
    if ans.citations:
        print("\nsources: " + ", ".join(ans.citations))
    if ans.verification and ans.verification.unsupported_claims:
        print("\n⚠ unverified claims (not supported by sources):")
        for claim in ans.verification.unsupported_claims:
            print(f"   - {claim}")
    return 0


def cmd_tui(_args: argparse.Namespace) -> int:
    from . import tui
    return tui.run()


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
    ph.add_argument("--m365", action="store_true",
                    help="use live M365 tools via the broker instead of mock tools")
    ph.set_defaults(func=cmd_handle)

    pb = sub.add_parser("heartbeat", help="proactive always-on tick")
    pb.add_argument("--watch", default="scan for urgent follow-ups",
                    help="the watch goal to run")
    pb.add_argument("--m365", action="store_true",
                    help="use live M365 tools via the broker instead of mock tools")
    pb.set_defaults(func=cmd_heartbeat)

    pm = sub.add_parser("remember", help="store a durable fact/preference")
    pm.add_argument("fact", help="the fact text")
    pm.add_argument("--kind", default="preference",
                    choices=["preference", "fact", "decision", "skill", "note"])
    pm.set_defaults(func=cmd_remember)

    pap = sub.add_parser("approvals", help="list pending held actions (persisted)")
    pap.add_argument("--m365", action="store_true",
                     help="use the M365 broker registry")
    pap.set_defaults(func=cmd_approvals)

    pav = sub.add_parser("approve", help="approve + execute a held action by id")
    pav.add_argument("approval_id", help="the approval id (appr-xxxxxxxx)")
    pav.add_argument("--m365", action="store_true",
                     help="use the M365 broker registry")
    pav.set_defaults(func=cmd_approve)

    pin = sub.add_parser("ingest", help="ingest documents into the RAG knowledge base")
    pin.add_argument("paths", nargs="+",
                     help="file paths (pdf/docx/pptx/xlsx/eml/msg/html/txt/md/csv/json)")
    pin.set_defaults(func=cmd_ingest)

    prc = sub.add_parser("recall", help="semantic search over the RAG knowledge base")
    prc.add_argument("query", help="the search query")
    prc.add_argument("--k", type=int, default=5, help="number of results")
    prc.set_defaults(func=cmd_recall)

    pdsc = sub.add_parser("describe", help="extract text from a doc or media file")
    pdsc.add_argument("path", help="path to a document, image, audio, or video file")
    pdsc.set_defaults(func=cmd_describe)

    prt = sub.add_parser("route", help="show contextual model routing per role")
    prt.set_defaults(func=cmd_route)

    pask = sub.add_parser("ask", help="grounded Q&A over the KB + memory (cite or abstain)")
    pask.add_argument("question", help="the question to answer from sources")
    pask.add_argument("--k", type=int, default=5, help="sources to retrieve")
    pask.add_argument("--m365", action="store_true", help="use the M365 broker registry")
    pask.set_defaults(func=cmd_ask)

    pd = sub.add_parser("demo", help="run the bundled demo")
    pd.set_defaults(func=cmd_demo)

    po = sub.add_parser("onboard", help="pick a model provider + model (interactive)")
    po.add_argument("--provider", choices=["ollama", "openrouter", "github",
                                           "openai", "anthropic", "xai",
                                           "vercel-ai-gateway", "custom"],
                    help="non-interactive: provider id")
    po.add_argument("--model", help="non-interactive: model id")
    po.add_argument("--base-url", default=None, help="non-interactive: custom base URL")
    po.add_argument("--api-key", default=None,
                    help="non-interactive: paste key (else an env reference is used)")
    po.set_defaults(func=cmd_onboard)

    pt = sub.add_parser("tui", help="launch the interactive terminal UI")
    pt.set_defaults(func=cmd_tui)

    pm = sub.add_parser("m365", help="check the M365 broker connection + signed-in status")
    pm.set_defaults(func=cmd_m365)
    return parser


def _maybe_first_run_onboard(command: str) -> None:
    """Offer onboarding on first use when nothing is configured (TTY only)."""
    if command in ("onboard", "demo", "tui", "m365", "approvals", "approve",
                   "ingest", "recall", "describe", "route", "ask"):
        return
    if os.environ.get("PRAXIS_LLM"):   # explicit mode (mock/real/auto) — respect it
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
