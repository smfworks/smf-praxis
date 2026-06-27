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
import json
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


def cmd_mcp(args: argparse.Namespace) -> int:
    # Client mode: discover/inspect external MCP servers (stdlib, no 'mcp' pkg).
    if getattr(args, "list", False) or getattr(args, "probe", None):
        from . import config as cfg
        servers = (cfg.load_config().get("agents", {})
                   .get("mcp", {}).get("servers", {}) or {})
        if args.probe:
            from .mcp_client import MCPClient, mcp_tools
            sc = servers.get(args.probe)
            if not sc:
                print(f"no MCP server '{args.probe}' under agents.mcp.servers")
                return 1
            command = sc.get("command")
            if isinstance(command, str):
                command = [command, *sc.get("args", [])]
            client = MCPClient.connect_stdio(command, env=sc.get("env"))
            try:
                client.initialize()
                tools = mcp_tools(client, server_name=args.probe,
                                  risk_overrides=sc.get("risk"))
                print(f"{args.probe}: {len(tools)} tool(s) "
                      f"(server: {client.server_info.get('name', '?')})")
                for t in tools:
                    print(f"  [{t.risk.value:11}] {t.name}  — {t.description[:70]}")
            finally:
                client.close()
            return 0
        if not servers:
            print("no MCP servers configured (set agents.mcp.servers in praxis.json)")
            return 0
        for name, sc in servers.items():
            state = "enabled" if sc.get("enabled", True) else "disabled"
            print(f"{name:16} {state:9} {sc.get('command')}")
        return 0

    # Server mode (default): expose Praxis tools over stdio (needs the 'mcp' pkg).
    import asyncio

    from .mcp_adapter import run_stdio_server
    from .tools import default_registry
    asyncio.run(run_stdio_server(default_registry(), name="praxis"))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon, daemon_logs, daemon_status
    action = args.action or "status"
    if action == "start":
        daemon = Daemon.from_env(work_dir=args.work_dir, status_port=args.port)
        return daemon.start()
    if action == "status":
        status = daemon_status()
        print(json.dumps(status, indent=2, default=str))
        return 0
    if action == "stop":
        status = daemon_status()
        if not status.get("running"):
            print("daemon not running")
            return 0
        import urllib.request
        port = status.get("port")
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/stop", timeout=5
            )
        except Exception as exc:
            print(f"could not stop daemon: {exc}")
            return 1
        print("stop requested")
        return 0
    if action == "logs":
        print(daemon_logs(lines=args.lines))
        return 0
    if action == "submit":
        status = daemon_status()
        if not status.get("running"):
            print("daemon not running")
            return 1
        import urllib.request
        port = status.get("port")
        if port is None:
            # Fall back to the on-disk port file if status didn't return it.
            from .daemon import _read_pid
            port = _read_pid()
        body = json.dumps({"goal": args.goal, "max_attempts": args.max_attempts}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/submit",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(resp.read().decode())
        except Exception as exc:
            print(f"could not submit: {exc}")
            return 1
        return 0
    print(f"unknown daemon action: {action}")
    return 1


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
        if p.rationale:
            print(f"      rationale: {p.rationale}")
        if p.evidence:
            sources = ", ".join(e.get("source", "?") for e in p.evidence[:5])
            print(f"      evidence: {sources}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    print(agent.approve(args.approval_id, approved_by=args.approved_by,
                        approval_notes=args.notes or ""))
    return 0


def cmd_compliance(_args: argparse.Namespace) -> int:
    from .compliance import ComplianceReporter
    from .persistence import Store
    reporter = ComplianceReporter(Store.open())
    print(ComplianceReporter.render(reporter.build()))
    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .task_manager import TaskManager
    task = TaskManager(Store.open()).create(args.goal, max_attempts=args.max_attempts)
    print(f"created {task.task_id} [{task.status}] {task.goal}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .task_manager import TaskManager
    tasks = TaskManager(Store.open()).list(status=args.status, limit=args.limit)
    if not tasks:
        print("no tasks")
        return 0
    for t in tasks:
        extra = f" cycle={t.cycle_id}" if t.cycle_id else ""
        print(f"{t.task_id} [{t.status}] attempts={t.attempts}/{t.max_attempts}{extra} :: {t.goal}")
    return 0


def cmd_task_run(args: argparse.Namespace) -> int:
    from .task_manager import TaskManager
    agent = _make_agent(args)
    task = TaskManager(agent.store).run_once(args.task_id, agent)
    print(f"{task.task_id} [{task.status}] attempts={task.attempts}/{task.max_attempts}")
    if task.cycle_id:
        print(f"cycle: {task.cycle_id}")
    if task.error:
        print(f"error: {task.error}")
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .task_manager import TaskManager
    ok = TaskManager(Store.open()).cancel(args.task_id)
    print("cancelled" if ok else "not cancelled")
    return 0


def cmd_wiki_add(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .wiki import KBSourceManager
    from .wiki_safe import UnsafeSourceError
    interval = KBSourceManager.seconds_from_hours(args.refresh_hours)
    try:
        src = KBSourceManager(Store.open()).add(
            args.uri, ns=args.ns, title=args.title or "",
            refresh_interval_seconds=interval)
    except UnsafeSourceError as exc:
        print(f"refused: {exc}")
        return 1
    print(f"registered {src.source_id} [{src.source_type}] ns={src.ns} {src.uri}")
    return 0


def cmd_wiki_sources(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .wiki import KBSourceManager
    sources = KBSourceManager(Store.open()).list(enabled=None if args.all else True)
    if not sources:
        print("no KB/wiki sources")
        return 0
    for src in sources:
        last = f" last={int(src.last_ingested_ts)}" if src.last_ingested_ts else ""
        print(f"{src.source_id} [{src.status}] ns={src.ns}{last} :: {src.uri}")
        if src.error:
            print(f"   error: {src.error}")
    return 0


def cmd_wiki_refresh(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .rag import Rag
    from .wiki import KBSourceManager
    store = Store.open()
    mgr = KBSourceManager(store)
    if args.source_id:
        refreshed = [mgr.refresh(args.source_id, rag=Rag(store))]
    else:
        refreshed = mgr.refresh_due(rag=Rag(store))
    if not refreshed:
        print("no sources due for refresh")
        return 0
    for src in refreshed:
        print(f"{src.source_id} [{src.status}] {src.uri}")
        if src.error:
            print(f"   error: {src.error}")
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
    store = Store.open()
    if getattr(args, "memory", False):
        from .memory import Memory
        mem_hits = Memory(store=store).recall(args.query, k=args.k)
        if not mem_hits:
            print("no matching memory (add facts with 'praxis remember')")
            return 0
        for it in mem_hits:
            snippet = " ".join(it.text.split())[:200]
            print(f"[{it.kind}] {it.provenance}  {snippet}")
        return 0
    from .rag import Rag
    rag = Rag(store)
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
    from . import config as cfg
    tiers = cfg.load_config().get("agents", {}).get("tiers", {})
    if tiers:
        print()
        print("difficulty tiers (agents.tiers): "
              f"fast={tiers.get('fast', '-')}  "
              f"balanced={tiers.get('balanced', '-')}  "
              f"strong={tiers.get('strong', '-')}")
        print("hard turns prefer 'strong', simple turns 'fast'; "
              "sensitivity still pins to a local model.")
    return 0


def cmd_debate(args: argparse.Namespace) -> int:
    from .debate import DebatePanel
    from .llm import LLMClient
    llm = LLMClient()

    def solver(task: str, stance: str) -> str:
        return llm.chat([{"role": "user", "content": task}], system=stance,
                        role="general")

    result = DebatePanel(solver).debate(args.question)
    print(result.answer)
    print()
    print(f"— {result.rationale}")
    if args.verbose:
        for c in result.candidates:
            mark = "ok" if c.approved else "xx"
            print(f"  [{mark}] {c.stance[:34]:34}  {' '.join(c.answer.split())[:110]}")
    return 0


def cmd_router_train(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator
    from .persistence import Store
    from .router_model import samples_from_runs
    store = Store.open()
    try:
        orch = Orchestrator(store)
        model = orch.train_router(min_samples=args.min_samples)
        runs = store.list_subagent_runs(limit=1000)
        if model is None:
            usable = len(samples_from_runs(runs))
            print(f"not enough subagent-run history to train the learned router "
                  f"(have {usable} successful runs across "
                  f"{len({r['role'] for r in runs})} role(s); need "
                  f">= {args.min_samples} across >= 2 roles).")
            print("the keyword heuristic stays in effect.")
            return 1
        counts: dict[str, int] = {}
        for _goal, role in samples_from_runs(runs):
            counts[role] = counts.get(role, 0) + 1
        print(f"trained learned goal->role router on {model.n_samples} runs "
              f"({len(model.classes)} roles, vocab={len(model.vocab)} tokens). "
              "saved to the store; orchestration will use it on the next run.")
        for role in sorted(counts):
            print(f"  {role:<12} {counts[role]} example(s)")
        if args.goal:
            predicted, conf = model.predict(args.goal)
            print(f"\npredict({args.goal!r}) -> {predicted} "
                  f"(confidence {conf:.2f})")
        return 0
    finally:
        store.close()


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
    if getattr(ans, "contradictions", None):
        print("\n⚠ contradictions detected across retrieved sources:")
        for c in ans.contradictions:
            print(f"   [{c.score:.2f}] {c.a_source} <-> {c.b_source}: "
                  f"{c.explanation}")
    return 0


def cmd_health(_args: argparse.Namespace) -> int:
    from .metrics import HealthMonitor
    from .persistence import Store
    snap = HealthMonitor(Store.open()).snapshot()
    print(HealthMonitor.render(snap))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import json

    from .evals import run_evals

    if getattr(args, "history", 0):
        import datetime

        from .persistence import Store
        store = Store.open()
        try:
            runs = store.list_eval_runs(limit=args.history)
        finally:
            store.close()
        if not runs:
            print("no saved eval runs yet (use 'praxis eval --save').")
            return 0
        for r in runs:
            when = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
            print(f"#{r['id']:<4} {when}  {r['passes']}/{r['total']}")
        return 0

    report = run_evals(category=args.category)
    data = report.to_dict()

    if getattr(args, "json", None) is not None:
        text = json.dumps(data, indent=2)
        if args.json:
            from pathlib import Path
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"wrote {args.json}")
        else:
            print(text)
    else:
        print(report.render())

    rc = 0 if report.passed else 1
    if args.save or args.set_baseline or args.check:
        from .persistence import Store
        store = Store.open()
        try:
            if args.save or args.set_baseline:
                store.save_eval_run(json.dumps(data), report.passes, report.total)
            if args.set_baseline:
                store.save_eval_baseline(json.dumps(data))
                print("baseline saved.")
            if args.check:
                from .eval_history import compare_reports
                base = store.load_eval_baseline()
                if base is None:
                    print("no baseline set (run 'praxis eval --set-baseline').")
                else:
                    rr = compare_reports(base, data)
                    print(rr.render())
                    if not rr.ok:
                        rc = 2
        finally:
            store.close()
    return rc


def cmd_memory_purge(args: argparse.Namespace) -> int:
    from .memory import Memory
    from .persistence import Store
    mem = Memory(store=Store.open())
    removed = mem.purge_expired()
    if args.decay_days is not None:
        removed += mem.decay_episodic(max_age_days=args.decay_days,
                                      salience_floor=args.salience_floor)
    if args.forget_provenance:
        removed += mem.forget_by_provenance(args.forget_provenance)
    print(f"removed {removed} memory item(s)")
    return 0


def cmd_scratchpad_read(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .scratchpad import Scratchpad
    entries = Scratchpad(Store.open()).read(args.key, ns=args.ns)
    if not entries:
        print(f"no scratchpad entries for {args.ns}/{args.key}")
        return 0
    for e in entries:
        print(f"[{e.written_by}] {e.value}")
    return 0


def cmd_scratchpad_write(args: argparse.Namespace) -> int:
    from .persistence import Store
    from .scratchpad import Scratchpad
    Scratchpad(Store.open()).write(args.key, args.value,
                                   written_by=args.written_by, ns=args.ns,
                                   ttl_seconds=args.ttl)
    print("written")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    if agent.skills is None:
        print("no skill store available")
        return 1
    draft = agent.learn_skill(args.goal, name=args.name)
    print("Drafted skill (governed — requires approval to save):\n")
    print(draft.to_markdown())
    save = args.yes
    if not save and sys.stdin.isatty():
        save = input("Save this skill? [y/N]: ").strip().lower() == "y"
    if save:
        path = agent.skills.add(draft)
        print(f"\nsaved skill '{draft.name}' -> {path}")
    else:
        print("\nnot saved. Skills require approval; re-run with --yes to persist.")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    skills = agent.skills.list() if agent.skills else []
    if not skills:
        print("no skills saved yet (use 'praxis learn \"<goal>\"')")
        return 0
    print(f"{len(skills)} skill(s):")
    for sk in skills:
        state = "" if sk.enabled else " (disabled)"
        print(f"  - {sk.name}{state}: {sk.trigger}")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    sk = agent.skills.get(args.name) if agent.skills else None
    if not sk:
        print(f"no skill named '{args.name}'")
        return 1
    print(sk.to_markdown())
    return 0


def cmd_skill_record(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    from .skill_evaluator import SkillEvaluator
    ev = SkillEvaluator(agent.skills)
    ev.record(args.name, args.goal, args.outcome, cycle_id=args.cycle_id or "",
              notes=args.notes or "")
    print(ev.impact_report(args.name))
    return 0


def cmd_skill_evaluate(args: argparse.Namespace) -> int:
    agent = _make_agent(args)
    from .skill_evaluator import SkillEvaluator
    ev = SkillEvaluator(agent.skills)
    quarantined = ev.quarantine_low_quality(
        min_uses=args.min_uses, threshold=args.threshold)
    if quarantined:
        print("quarantined: " + ", ".join(quarantined))
    metas = agent.skills.rag.store.list_skill_metadata() if agent.skills and agent.skills.rag else []
    if not metas:
        print("no skill outcome data")
        return 0
    for m in metas:
        print(ev.impact_report(m["skill_name"]))
    return 0


def cmd_subagent_run(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator
    from .persistence import Store
    run = Orchestrator(Store.open()).run(args.goal, role=args.role)
    print(f"{run.run_id} [{run.status}] role={run.role} agent={run.agent_id}")
    if run.cycle_id:
        print(f"cycle: {run.cycle_id}")
    return 0


def cmd_fanout(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator
    from .persistence import Store
    runs = Orchestrator(Store.open()).run_many(args.goals, max_workers=args.workers)
    for run in runs:
        print(f"{run.run_id} [{run.status}] role={run.role} agent={run.agent_id}")
    failed = sum(1 for r in runs if r.status == "failed")
    print(f"\n{len(runs)} subagent(s) ran concurrently; {failed} failed.")
    return 1 if failed else 0


def cmd_subagents(args: argparse.Namespace) -> int:
    from .orchestrator import AgentPool, Orchestrator
    from .persistence import Store
    store = Store.open()
    agents = AgentPool(store).list()
    if not agents:
        print("no subagents registered yet")
    else:
        print("subagents:")
        for a in agents:
            print(f"  {a.agent_id} [{a.role}] tools={','.join(a.tools)}")
    runs = Orchestrator(store).list_runs(limit=args.limit)
    if runs:
        print("runs:")
        for r in runs:
            print(f"  {r['run_id']} [{r['status']}] role={r['role']} :: {r['goal']}")
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
    from . import demo
    demo.main()
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
    pav.add_argument("--approved-by", default="user",
                     help="operator identity recorded in the audit trail")
    pav.add_argument("--notes", default="", help="approval notes/justification")
    pav.add_argument("--m365", action="store_true",
                     help="use the M365 broker registry")
    pav.set_defaults(func=cmd_approve)

    pc = sub.add_parser("compliance", help="render a compliance attestation report")
    pc.set_defaults(func=cmd_compliance)

    ptc = sub.add_parser("task-create", help="create a persistent resumable task")
    ptc.add_argument("goal", help="goal text")
    ptc.add_argument("--max-attempts", type=int, default=3)
    ptc.set_defaults(func=cmd_task_create)

    pts = sub.add_parser("tasks", help="list persistent tasks")
    pts.add_argument("--status", default=None, help="filter by status")
    pts.add_argument("--limit", type=int, default=50)
    pts.set_defaults(func=cmd_tasks)

    ptr = sub.add_parser("task-run", help="run one persistent task attempt")
    ptr.add_argument("task_id")
    ptr.add_argument("--m365", action="store_true", help="use the M365 broker registry")
    ptr.set_defaults(func=cmd_task_run)

    ptx = sub.add_parser("task-cancel", help="cancel a persistent task")
    ptx.add_argument("task_id")
    ptx.set_defaults(func=cmd_task_cancel)

    pwa = sub.add_parser("wiki-add", help="register a KB/wiki source for revalidation")
    pwa.add_argument("uri", help="file path or URL")
    pwa.add_argument("--ns", default="kb", help="RAG namespace")
    pwa.add_argument("--title", default="", help="display title")
    pwa.add_argument("--refresh-hours", type=float, default=None,
                     help="refresh interval in hours")
    pwa.set_defaults(func=cmd_wiki_add)

    pws = sub.add_parser("wiki-sources", help="list registered KB/wiki sources")
    pws.add_argument("--all", action="store_true", help="include disabled sources")
    pws.set_defaults(func=cmd_wiki_sources)

    pwr = sub.add_parser("wiki-refresh", help="refresh due KB/wiki sources")
    pwr.add_argument("source_id", nargs="?", default=None,
                     help="specific source id (default: all due)")
    pwr.set_defaults(func=cmd_wiki_refresh)

    pin = sub.add_parser("ingest", help="ingest documents into the RAG knowledge base")
    pin.add_argument("paths", nargs="+",
                     help="file paths (pdf/docx/pptx/xlsx/eml/msg/html/txt/md/csv/json)")
    pin.set_defaults(func=cmd_ingest)

    prc = sub.add_parser("recall",
                         help="semantic search over the RAG KB (or --memory)")
    prc.add_argument("query", help="the search query")
    prc.add_argument("--k", type=int, default=5, help="number of results")
    prc.add_argument("--memory", action="store_true",
                     help="BM25 search the agent's durable/episodic memory "
                          "instead of the KB (no embedding model needed)")
    prc.set_defaults(func=cmd_recall)

    pdsc = sub.add_parser("describe", help="extract text from a doc or media file")
    pdsc.add_argument("path", help="path to a document, image, audio, or video file")
    pdsc.set_defaults(func=cmd_describe)

    prt = sub.add_parser("route", help="show contextual model routing per role")
    prt.set_defaults(func=cmd_route)

    prtr = sub.add_parser(
        "router-train",
        help="train the learned goal->role router from subagent-run outcomes")
    prtr.add_argument("--min-samples", type=int, default=8,
                      help="minimum successful runs required to train (default 8)")
    prtr.add_argument("--goal", default="",
                      help="optional goal to test-predict after training")
    prtr.set_defaults(func=cmd_router_train)

    pdeb = sub.add_parser(
        "debate", help="best-of-N self-consistency answer judged across stances")
    pdeb.add_argument("question", help="the question to answer")
    pdeb.add_argument("--verbose", action="store_true",
                      help="show each solver's candidate and verification mark")
    pdeb.set_defaults(func=cmd_debate)

    pask = sub.add_parser("ask", help="grounded Q&A over the KB + memory (cite or abstain)")
    pask.add_argument("question", help="the question to answer from sources")
    pask.add_argument("--k", type=int, default=5, help="sources to retrieve")
    pask.add_argument("--m365", action="store_true", help="use the M365 broker registry")
    pask.set_defaults(func=cmd_ask)

    pl = sub.add_parser("learn", help="distill a reusable skill from a goal (/learn)")
    pl.add_argument("goal", help="the goal to learn a skill from")
    pl.add_argument("--name", default=None, help="override the skill name")
    pl.add_argument("--yes", action="store_true", help="approve + save without prompting")
    pl.add_argument("--m365", action="store_true", help="use the M365 broker registry")
    pl.set_defaults(func=cmd_learn)

    psk = sub.add_parser("skills", help="list saved skills")
    psk.set_defaults(func=cmd_skills)

    pskw = sub.add_parser("skill", help="show a saved skill by name")
    pskw.add_argument("name", help="the skill name")
    pskw.set_defaults(func=cmd_skill)

    psr = sub.add_parser("skill-record", help="record a skill outcome")
    psr.add_argument("name")
    psr.add_argument("goal")
    psr.add_argument("outcome", choices=["success", "partial", "failure"])
    psr.add_argument("--cycle-id", default="")
    psr.add_argument("--notes", default="")
    psr.set_defaults(func=cmd_skill_record)

    pse = sub.add_parser("skill-evaluate", help="evaluate and quarantine low-quality skills")
    pse.add_argument("--min-uses", type=int, default=3)
    pse.add_argument("--threshold", type=float, default=0.4)
    pse.set_defaults(func=cmd_skill_evaluate)

    psa = sub.add_parser("subagent-run", help="route a goal to a scoped subagent")
    psa.add_argument("goal")
    psa.add_argument("--role", default=None,
                     choices=["researcher", "drafter", "compliance", "predictor"],
                     help="force a subagent role; default predicts from the goal")
    psa.set_defaults(func=cmd_subagent_run)

    pfo = sub.add_parser("fanout",
                         help="run several goals concurrently as scoped subagents")
    pfo.add_argument("goals", nargs="+", help="goals to run in parallel")
    pfo.add_argument("--workers", type=int, default=4,
                     help="max concurrent workers (default 4)")
    pfo.set_defaults(func=cmd_fanout)

    psl = sub.add_parser("subagents", help="list scoped subagents and recent runs")
    psl.add_argument("--limit", type=int, default=20)
    psl.set_defaults(func=cmd_subagents)

    phl = sub.add_parser("health", help="render runtime health/metrics snapshot")
    phl.set_defaults(func=cmd_health)

    pev = sub.add_parser("eval", help="run the offline capability + safety eval suite")
    pev.add_argument("--category", default=None,
                     help="only run cases in this category "
                          "(tool_use, approval, safety, schema)")
    pev.add_argument("--json", nargs="?", const="", default=None, metavar="PATH",
                     help="emit the scorecard as JSON (to PATH, or stdout)")
    pev.add_argument("--save", action="store_true",
                     help="append this run to the persisted eval history")
    pev.add_argument("--set-baseline", action="store_true",
                     help="save this run as the regression baseline")
    pev.add_argument("--check", action="store_true",
                     help="compare to the baseline; exit 2 on any regression")
    pev.add_argument("--history", type=int, nargs="?", const=10, default=0,
                     metavar="N", help="show the last N saved runs and exit")
    pev.set_defaults(func=cmd_eval)

    pmp = sub.add_parser("memory-purge",
                         help="purge expired/old memory by retention policy")
    pmp.add_argument("--decay-days", type=float, default=None,
                     help="forget episodic items older than N days under salience floor")
    pmp.add_argument("--salience-floor", type=float, default=0.2)
    pmp.add_argument("--forget-provenance", default=None,
                     help="bulk-delete items whose provenance starts with this prefix")
    pmp.set_defaults(func=cmd_memory_purge)

    psr = sub.add_parser("scratchpad-read",
                         help="read scoped inter-subagent scratchpad entries")
    psr.add_argument("key")
    psr.add_argument("--ns", default="default")
    psr.set_defaults(func=cmd_scratchpad_read)

    psw = sub.add_parser("scratchpad-write",
                         help="write a scratchpad note from one subagent role")
    psw.add_argument("key")
    psw.add_argument("value")
    psw.add_argument("--written-by", default="cli")
    psw.add_argument("--ns", default="default")
    psw.add_argument("--ttl", type=float, default=3600.0)
    psw.set_defaults(func=cmd_scratchpad_write)

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

    pmcp = sub.add_parser("mcp",
                          help="MCP: run the Praxis server, or --list/--probe clients")
    pmcp.add_argument("--list", action="store_true",
                      help="list configured external MCP servers")
    pmcp.add_argument("--probe", metavar="NAME",
                      help="connect to a configured MCP server and list its tools")
    pmcp.set_defaults(func=cmd_mcp)

    pdm = sub.add_parser("daemon", help="long-running task worker")
    pdm.add_argument("action", nargs="?", choices=["start", "stop", "status", "logs", "submit"],
                     help="daemon action")
    pdm.add_argument("--port", type=int, default=None, help="control HTTP port")
    pdm.add_argument("--work-dir", default=None, help="working directory")
    pdm.add_argument("--goal", default="", help="goal to submit (with submit action)")
    pdm.add_argument("--max-attempts", type=int, default=3)
    pdm.add_argument("--lines", type=int, default=100, help="log lines to fetch")
    pdm.set_defaults(func=cmd_daemon)

    return parser


def _maybe_first_run_onboard(command: str) -> None:
    """Offer onboarding on first use when nothing is configured (TTY only)."""
    if command in ("onboard", "demo", "tui", "m365", "mcp", "daemon",
                   "approvals", "approve", "ingest", "recall", "describe", "route", "ask",
                   "learn", "skills", "skill", "compliance",
                   "skill-record", "skill-evaluate",
                   "subagent-run", "subagents",
                   "task-create", "tasks", "task-run", "task-cancel",
                   "wiki-add", "wiki-sources", "wiki-refresh",
                   "health", "memory-purge",
                   "scratchpad-read", "scratchpad-write"):
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
