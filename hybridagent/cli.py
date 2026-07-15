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
from .providers import CATALOG, ORDER, discover_ollama_models


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


def cmd_goal(args: argparse.Namespace) -> int:
    """Level 1 Goal Runner (H10): loop the agent until the independent
    verifier confirms the goal is met or the turn budget is spent."""
    from .goal_runner import GoalRunner
    from .verifier import VerificationConfig
    agent = _make_agent(args)
    vc = VerificationConfig.load()
    verifier = None
    if vc.enabled:
        from .verifier import AnswerVerifier
        verifier = AnswerVerifier(critic=vc.critic)
    runner = GoalRunner(agent, max_turns=args.max_turns, verifier=verifier,
                        threshold=args.threshold)
    result = runner.run(args.goal, approve_all=args.approve_all)
    # Surface each turn's progress (fights cognitive surrender).
    for t in result.turns:
        print(f"  turn {t.turn}/{result.max_turns}: {t.verdict} "
              f"progress={t.progress:.3f} ({t.report.summary()})")
    print(f"\n{result.summary()}")
    if args.json:
        print(json.dumps(runner.to_record(result), indent=2))
    # Exit code: 0 if approved, 1 if max_turns/blocked/error.
    return 0 if result.stopped_reason == "approved" else 1


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
    # Preset management: prebuilt server configs (e.g. xAI Docs MCP).
    if getattr(args, "list_presets", False):
        from . import mcp_presets
        for name in mcp_presets.preset_names():
            p = mcp_presets.get_preset(name) or {}
            target = p.get("url") or p.get("command")
            print(f"{name:14} {target}\n               {p.get('description', '')}")
        return 0
    if getattr(args, "enable_preset", None):
        from . import mcp_presets
        res = mcp_presets.enable_preset(args.enable_preset)
        if res.get("error"):
            print(res["error"])
            return 1
        print(f"enabled MCP preset '{res['enabled']}' "
              f"(probe it: praxis mcp --probe {res['enabled']})")
        return 0

    # Client mode: discover/inspect external MCP servers (stdlib, no 'mcp' pkg).
    if getattr(args, "list", False) or getattr(args, "probe", None):
        from . import config as cfg
        servers = (cfg.load_config().get("agents", {})
                   .get("mcp", {}).get("servers", {}) or {})
        if args.probe:
            from .mcp_client import MCPClient, _expand_env, mcp_tools
            sc = servers.get(args.probe)
            if not sc:
                print(f"no MCP server '{args.probe}' under agents.mcp.servers")
                return 1
            url = sc.get("url")
            if url:
                client = MCPClient.connect_http(
                    url, headers=_expand_env(sc.get("headers") or {}))
            else:
                command = sc.get("command")
                if isinstance(command, str):
                    command = [command, *sc.get("args", [])]
                client = MCPClient.connect_stdio(
                    command, env=_expand_env(sc.get("env")))
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
            print("no MCP servers configured (set agents.mcp.servers in praxis.json"
                  " or run: praxis mcp --enable-preset xai-docs)")
            return 0
        for name, sc in servers.items():
            state = "enabled" if sc.get("enabled", True) else "disabled"
            target = sc.get("url") or sc.get("command")
            print(f"{name:16} {state:9} {target}")
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
        daemon = Daemon.from_env(work_dir=args.work_dir, status_port=args.port,
                                 status_host=getattr(args, "host", None))
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
        import urllib.error
        import urllib.request
        port = status.get("port")
        if port is None:
            try:
                from .config import home_dir
                pfile = home_dir() / "daemon.port"
                if pfile.exists():
                    port = int(pfile.read_text().strip())
            except Exception:
                port = None
        if not port:
            print("could not stop daemon: no control port found")
            return 1
        try:
            # /stop is registered on do_POST only; bare GET returns 404.
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            # Server often drops the connection while shutting down.
            msg = str(exc).lower()
            if any(s in msg for s in (
                "connection reset", "remotely closed", "errno 104",
                "connection refused", "urlopen error",
            )):
                print("stop requested")
                return 0
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


def _parse_duration(text: str) -> float | None:
    """Parse '90s', '15m', '1h', '1h30m', or a bare seconds number -> seconds."""
    import re
    text = (text or "").strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total, matched = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhd])", text):
        matched = True
        total += float(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return total if matched else None


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"


def cmd_governance(args: argparse.Namespace) -> int:
    """View or set the governance compliance mode via the running daemon."""
    import urllib.request

    from .daemon import daemon_status
    status = daemon_status()
    if not status.get("running"):
        print("daemon not running (start it with: praxis daemon start)")
        return 1
    port = status.get("port")
    action = (args.action or "status").lower()
    url = f"http://127.0.0.1:{port}/api/compliance"
    try:
        if action == "status":
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
        else:
            body: dict = {"mode": action}
            if getattr(args, "for_", None):
                ttl = _parse_duration(args.for_)
                if ttl is None:
                    print(f"could not parse duration '{args.for_}' (try 30m, 1h, 90s)")
                    return 1
                body["ttl_seconds"] = ttl
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"could not reach daemon: {exc}")
        return 1
    if data.get("error"):
        print(f"error: {data['error']}")
        return 1
    line = f"compliance mode: {data.get('mode')}"
    secs = data.get("expires_in_seconds")
    if secs:
        line += f"  (auto-reverts to enforced in {_fmt_duration(secs)})"
    print(line)
    for m in data.get("modes", []):
        mark = "*" if m.get("active") else " "
        print(f"  {mark} {m['id']:<11} {m.get('description', '')}")
    return 0


def _is_editable_install() -> bool:
    """True when praxis-agent runs from a source/editable checkout rather than a
    normal site-packages install (so `pip install --upgrade` shouldn't clobber it)."""
    try:
        import importlib.metadata as im
        durl = im.distribution("praxis-agent").read_text("direct_url.json")
        if durl and json.loads(durl).get("dir_info", {}).get("editable"):
            return True
    except Exception:
        pass
    # Legacy `pip install -e` (egg-link/.pth) or running straight from a clone:
    # the package directory won't live under a site-packages directory.
    try:
        from pathlib import Path

        import hybridagent
        pkg = Path(hybridagent.__file__).resolve().parent
        return "site-packages" not in pkg.parts
    except Exception:
        return False


def _latest_github_version() -> str | None:
    """Newest stable Praxis GitHub Release version, or ``None`` if unavailable."""
    import re
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com/repos/smfworks/smf-praxis/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "praxis-agent-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        tag = payload.get("tag_name") if type(payload) is dict else None
        match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag) if type(tag) is str else None
        return match.group(1) if match is not None else None
    except Exception:
        return None


def _release_version_key(version: str) -> tuple[int, int, int]:
    """Return a comparable key for a validated three-part release version."""
    import re

    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("invalid GitHub release version")
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _github_release_wheel_url(version: str) -> str:
    """Return the exact pure-Python wheel URL for a validated release version."""
    _release_version_key(version)
    return (
        "https://github.com/smfworks/smf-praxis/releases/download/"
        f"v{version}/praxis_agent-{version}-py3-none-any.whl"
    )


def cmd_update(args: argparse.Namespace) -> int:
    """Upgrade from the latest GitHub Release wheel and migrate config."""
    import subprocess

    from . import __version__
    if _is_editable_install():
        print(f"praxis {__version__} is an editable/source checkout — update with "
              "`git pull` (then re-run ./install.sh if needed), not `praxis update`.")
        return 1
    latest = _latest_github_version()
    if args.check:
        if latest is None:
            print(
                f"praxis {__version__} "
                "(could not reach GitHub Releases to check for updates)"
            )
        elif _release_version_key(latest) <= _release_version_key(__version__):
            print(f"praxis {__version__} is up to date.")
        else:
            print(f"praxis {__version__} -> {latest} available. Run `praxis update`.")
        return 0
    if latest is None:
        print(
            "Update unavailable: could not resolve GitHub Releases. "
            "No package was installed."
        )
        return 1
    if _release_version_key(latest) <= _release_version_key(__version__):
        print(f"praxis {__version__} is already up to date.")
        cfg.migrate_config()
        return 0
    wheel_url = _github_release_wheel_url(latest)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url]
    print("Updating from GitHub Release:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        migrated = cfg.migrate_config()
        if migrated:
            print(f"Config migrated to v{migrated}.")
        print("Updated. Run `praxis --version` to confirm.")
    else:
        print("Update failed.")
    return rc


def cmd_secrets(args: argparse.Namespace) -> int:
    """Inspect and manage stored provider API keys (OS keychain / gitignored file)."""
    action = (args.action or "status").lower()
    if action == "status":
        if cfg.keychain_available():
            print("keychain backend: available")
        else:
            print("keychain backend: unavailable — keys fall back to a gitignored "
                  "file (install the 'keyring' extra for OS-keychain storage)")
        providers = cfg.load_config().get("providers", {})
        if not providers:
            print("no providers configured.")
            return 0
        for pid in sorted(providers):
            print(f"  {pid:<16} {cfg.key_location(pid)}")
        return 0
    if action == "migrate":
        if not cfg.keychain_available():
            print("no keychain backend available (install the 'keyring' extra); "
                  "nothing migrated.")
            return 1
        moved = cfg.migrate_secrets_to_keychain()
        print(f"migrated {moved} key(s) from the plaintext file to the OS keychain.")
        return 0
    if action in ("set", "rm") and not args.provider:
        print(f"usage: praxis secrets {action} --provider <id>")
        return 1
    if action == "set":
        import getpass
        key = getpass.getpass(f"Paste API key for {args.provider}: ").strip()
        if not key:
            print("no key entered.")
            return 1
        backend = cfg.save_api_key(args.provider, key)
        print(f"stored key for {args.provider} in {backend}.")
        return 0
    if action == "rm":
        cfg.delete_api_key(args.provider)
        print(f"removed any stored key for {args.provider}.")
        return 0
    print(f"unknown secrets action: {action}")
    return 1


def cmd_pack(args: argparse.Namespace) -> int:
    """Manage vertical packs (bundle prompt + policy + tools for a domain)."""
    from . import pack
    action = (args.action or "list").lower()
    if action == "list":
        packs = pack.list_packs()
        active_name = cfg.get_active_pack_name()
        if not packs:
            print("no packs installed. Scaffold one with: praxis pack create <name>")
            return 0
        for name in sorted(packs):
            p = packs[name]
            mark = "*" if name == active_name else " "
            print(f"  {mark} {name:<16} {(p.vertical or ''):<12} {p.description[:48]}")
        return 0
    if action == "show":
        shown = pack.load_pack(args.name) if args.name else pack.active()
        if shown is None:
            print("pack not found (or no active pack).")
            return 1
        print(json.dumps(shown.to_manifest(), indent=2))
        return 0
    if action == "templates":
        from . import vertical_templates as vt
        print("built-in vertical templates (use: praxis pack create <name> --vertical <t>):")
        for key in vt.list_templates():
            t = vt.get_template(key) or {}
            mode = t.get("complianceMode", "enforced")
            print(f"  {key:<12} {mode:<10} {t.get('description', '')[:50]}")
        return 0
    if action == "create":
        if not args.name:
            print("usage: praxis pack create <name> [--vertical <template>]")
            return 1
        try:
            d = pack.create_pack(args.name, vertical=(args.vertical or ""))
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"created pack at {d}")
        return 0
    if action == "install":
        if not args.name:
            print("usage: praxis pack install <path>")
            return 1
        try:
            p = pack.install_pack(args.name)
            pack.activate(p.name)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"installed and activated '{p.name}'")
        return 0
    if action == "activate":
        if not args.name:
            print("usage: praxis pack activate <name>")
            return 1
        try:
            p = pack.activate(args.name)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        extra = f" (compliance: {p.compliance_mode})" if p.compliance_mode else ""
        kn = f"; ingested {len(p.knowledge)} knowledge source(s)" if p.knowledge else ""
        sk = f"; installed {len(p.skills)} skill(s)" if p.skills else ""
        print(f"activated '{p.name}'{extra}{kn}{sk}")
        return 0
    if action == "deactivate":
        pack.deactivate()
        print("deactivated; no active pack.")
        return 0
    print(f"unknown pack action: {action}")
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
    from .daemon import Daemon
    daemon = Daemon(store=agent.store, agent=agent)
    if agent.store is not None and agent.store.has_task_approval_action(args.approval_id):
        approved = daemon.approve(
            args.approval_id,
            approved_by=args.approved_by,
            approval_notes=args.notes or "",
        )
        row = agent.store.get_approval(args.approval_id) or {}
        print("task approval executed" if approved else f"task approval {row.get('status', 'pending')}")
    else:
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print the first-run readiness checklist (model/memory/search/wiki/...)."""
    from . import readiness
    print(readiness.render())
    rep = readiness.readiness()
    return 0 if rep["counts"].get("warn", 0) == 0 else 1


def cmd_message(args: argparse.Namespace) -> int:
    from . import gateways
    if getattr(args, "list", False):
        configured = gateways.configured_targets()
        print("available channels: " + ", ".join(gateways.available_channels()))
        print("configured channels: " + (", ".join(configured) or "none "
              "(set agents.gateways.<channel> in praxis.json)"))
        return 0
    if not args.target or not args.text:
        print("usage: praxis message <target> <text>   (e.g. telegram \"hi\")")
        return 1
    res = gateways.deliver(args.target, args.text)
    print(f"{'sent' if res.ok else 'FAILED'} via {res.channel}: {res.detail}")
    return 0 if res.ok else 1


def cmd_scan(args: argparse.Namespace) -> int:
    from . import security_scan
    target = getattr(args, "scan_target", None)
    if target == "skills":
        from .persistence import Store
        from .skills import SkillLibrary
        lib = SkillLibrary(store=Store.open())
        skills = lib.list()
        if not skills:
            print("no skills to scan")
            return 0
        any_dirty = False
        for sk in skills:
            rep = security_scan.scan_skill(sk)
            print(f"{'OK ' if rep.clean else '!! '}{rep.summary()}")
            any_dirty = any_dirty or not rep.clean
        return 1 if any_dirty else 0
    if target == "mcp":
        from . import config as cfg
        from .mcp_client import MCPClient, _expand_env
        servers = (cfg.load_config().get("agents", {})
                   .get("mcp", {}).get("servers", {}) or {})
        sc = servers.get(args.server)
        if not sc:
            print(f"no MCP server '{args.server}' configured")
            return 1
        url = sc.get("url")
        if url:
            client = MCPClient.connect_http(url, headers=_expand_env(sc.get("headers") or {}))
        else:
            command = sc.get("command")
            if isinstance(command, str):
                command = [command, *sc.get("args", [])]
            client = MCPClient.connect_stdio(command, env=_expand_env(sc.get("env")))
        try:
            client.initialize()
            result = security_scan.scan_mcp_tools(client.list_tools())
        finally:
            client.close()
        for _name, rep in result["reports"].items():
            print(f"{'OK ' if rep.clean else '!! '}{rep.summary()}")
        print(f"\nclean={result['clean']}  flagged={result['flagged']}")
        return 0 if result["clean"] else 1
    if target == "deps":
        import subprocess
        try:
            out = subprocess.check_output(["pip", "freeze"], text=True, timeout=30)
        except Exception as exc:
            print(f"could not list dependencies: {exc}")
            return 1
        pkgs = [(n.strip(), v.strip()) for line in out.splitlines()
                if "==" in line for n, _, v in [line.partition("==")]]
        if not pkgs:
            print("no pinned dependencies found")
            return 0
        print(f"checking {len(pkgs)} packages against OSV.dev ...")
        res = security_scan.osv_check(pkgs)
        if res.get("error"):
            print(f"OSV check error: {res['error']}")
            return 1
        affected = res.get("affected", {})
        if not affected:
            print("no known vulnerabilities found")
            return 0
        for pkg, vulns in affected.items():
            print(f"!! {pkg}: {', '.join(vulns)}")
        return 1
    print("usage: praxis scan {skills | mcp --server NAME | deps}")
    return 1


def cmd_cron(args: argparse.Namespace) -> int:
    from datetime import datetime

    from .cron import CronScheduler
    from .persistence import Store
    sched = CronScheduler(Store.open())
    action = getattr(args, "cron_action", None) or "list"
    if action == "add":
        job = sched.create(args.goal, args.schedule, name=args.name or "",
                           mode=args.mode, deliver=args.deliver)
        if "error" in job:
            print(f"error: {job['error']}")
            return 1
        nr = job.get("next_run_ts")
        when = datetime.fromtimestamp(nr).strftime("%Y-%m-%d %H:%M") if nr else "?"
        print(f"created {job['job_id']} [{job['mode']}] '{job['schedule']}' "
              f"-> next {when}; deliver={job['deliver']}")
        return 0
    if action == "remove":
        print("removed" if sched.delete(args.job_id) else "not found")
        return 0
    if action in ("pause", "resume"):
        ok = sched.set_enabled(args.job_id, action == "resume")
        print(f"{action}d" if ok else "not found")
        return 0
    jobs = sched.list()
    if not jobs:
        print("no cron jobs (add one: praxis cron add --schedule '0 9 * * *' \"goal\")")
        return 0
    for j in jobs:
        state = "on " if j["enabled"] else "off"
        nr = j.get("next_run_ts")
        when = datetime.fromtimestamp(nr).strftime("%m-%d %H:%M") if nr else "-"
        print(f"{j['job_id']} [{state}] {j['mode']:8} next={when} "
              f"'{j['schedule']}' runs={j['runs']} :: {j['goal'][:50]}")
        if j.get("last_status"):
            print(f"    last: {j['last_status']}  {(j.get('last_output') or '')[:80]}")
    return 0



def cmd_jobs(args: argparse.Namespace) -> int:
    """List or run the three first-class SMF vertical jobs."""
    from .jobs import get_job, list_jobs, run_research, schedule_colleague
    action = getattr(args, "jobs_action", None) or "list"
    if action == "list":
        for j in list_jobs():
            print(f"{j['id']:10}  {j['title']}")
            print(f"            {j['summary']}")
            print(f"            risk: {j['risk_note']}")
            print(f"            e.g.  {j['example_prompt'][:70]}...")
            print()
        print('run:  praxis jobs run research --query "..."')
        print('      praxis jobs run draft --prompt "..."   (needs running daemon for tools)')
        print('      praxis jobs run schedule --goal "..." --schedule "0 9 * * 1-5"')
        return 0
    if action == "run":
        job_id = args.job
        job = get_job(job_id)
        if job is None:
            print(f"unknown job: {job_id} (try: research | draft | schedule)")
            return 1
        if job_id == "research":
            from .daemon import Daemon
            d = Daemon.from_env()
            d._ensure_agent()
            q = args.query or job.example_prompt
            res = run_research(d, q)
            print(res.get("text") or res.get("error") or res)
            if res.get("citations"):
                print("citations:", ", ".join(res["citations"][:8]))
            return 0 if not res.get("blocked") else 2
        if job_id == "draft":
            from .daemon import Daemon
            d = Daemon.from_env()
            d._ensure_agent()
            prompt = args.prompt or job.example_prompt
            final = ""
            for ev in d.chat_agent([{"role": "user", "content": prompt}]):
                if ev.get("type") == "final":
                    final = ev.get("text") or final
                elif ev.get("type") == "approval":
                    print(f"held: {ev.get('tool')} — approve in the Command Deck")
                elif ev.get("type") == "error":
                    print("error:", ev.get("error"))
                    return 2
            print(final or "(no final text; check approvals if a send was held)")
            return 0
        if job_id == "schedule":
            from .persistence import Store
            goal = args.goal or job.example_prompt
            schedule = args.schedule or "0 9 * * 1-5"
            job_row = schedule_colleague(Store.open(), goal=goal, schedule=schedule,
                                         name=args.name or "colleague")
            if job_row.get("error"):
                print("error:", job_row["error"])
                return 1
            print(f"scheduled {job_row.get('job_id')} '{schedule}' :: {goal[:60]}")
            print("(daemon must be running for cron ticks)")
            return 0
    print("usage: praxis jobs list | praxis jobs run {research|draft|schedule} ...")
    return 1


def cmd_budget(args: argparse.Namespace) -> int:
    """Show or set the spend budget hard-stop."""
    from .persistence import Store
    store = Store.open()
    action = getattr(args, "budget_action", None) or "status"
    if action == "set":
        store.set_budget_limit(float(args.limit))
        b = store.get_budget()
        print(f"limit set to ${b['limit_usd']:.2f} (spent ${b['spent_usd']:.4f})")
        return 0
    if action == "reset":
        store.reset_budget()
        b = store.get_budget()
        print(f"spend reset (limit ${b['limit_usd']:.2f})")
        return 0
    b = store.get_budget()
    over = b["limit_usd"] > 0 and b["spent_usd"] >= b["limit_usd"]
    print(f"limit=${b['limit_usd']:.2f}  spent=${b['spent_usd']:.4f}  "
          f"runs={b['runs']}  over={over}")
    if b["limit_usd"] <= 0:
        print("hint: set a cap with  praxis budget set 5")
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


def cmd_plan_run(args: argparse.Namespace) -> int:
    from .plan_execute import PlanExecutor
    agent = _make_agent(args)
    # Plan steps are broker-authorized; make sure the registry's tools are allowed.
    agent.broker.policy.allowed_tools.update(agent.registry.names())
    report = PlanExecutor(agent.registry, agent.broker, store=agent.store,
                          max_replans=args.max_replans).execute(args.goal)
    tags = {"step_done": "[ok]  ", "step_held": "[hold]", "step_denied": "[deny]",
            "step_failed": "[fail]", "step_skipped": "[skip]", "replan": "[plan]"}
    for ev in report.events:
        if ev.type in ("plan", "final"):
            continue
        sid = ev.data.get("id", "")
        detail = ev.data.get("intent") or ev.data.get("reason") or ""
        print(f"  {tags.get(ev.type, ev.type):6} {sid:5} {detail}")
    print(report.summary())
    held = report.held_approvals()
    if held:
        print("held for approval: " + ", ".join(held)
              + "  (approve with 'praxis approve <id>')")
    return 1 if report.status == "failed" else 0


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


def cmd_think(args: argparse.Namespace) -> int:
    from .deepthink import DeepThink
    from .llm import LLMClient
    llm = LLMClient()

    def solver(task: str, directive: str) -> str:
        return llm.chat([{"role": "user", "content": task}],
                        system=directive or None, role="general")

    res = DeepThink(solver, rounds=args.rounds).solve(args.question, force=args.force)
    print(res.answer)
    print()
    if res.engaged:
        mark = "verified" if res.approved else "flagged by reviewer"
        print(f"— deep-think: {res.rounds} round(s), {res.votes} solver(s) agreed, "
              f"{mark}")
    else:
        print("— single pass (not classified hard; use --force to deliberate)")
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


def cmd_evolve(args: argparse.Namespace) -> int:
    from . import evolution as ev
    from .persistence import Store
    from .skills import SkillLibrary
    lib = SkillLibrary(store=Store.open())
    llm = None
    if getattr(args, "llm", False):
        from .llm import LLMClient
        llm = LLMClient()
    targets = [args.skill] if args.skill else [s.name for s in lib.list()]
    if not targets:
        print("no skills to evolve (use 'praxis learn' to create some)")
        return 0
    proposals = []
    for name in targets:
        prop = ev.evolve_skill(lib, name, llm=llm)
        if prop is not None:
            proposals.append(prop)
    if not proposals:
        print("no improving proposals found")
        return 0
    for p in proposals:
        print("\n" + p.summary())
        print(p.diff())
        if p.rationale:
            print(f"rationale: {p.rationale}")
    if getattr(args, "apply", False):
        applied = [p.skill_name for p in proposals if ev.apply_proposal(lib, p)]
        print(f"\napplied {len(applied)} proposal(s): {', '.join(applied)}")
    else:
        print("\n(propose-only; re-run with --apply to accept after review)")
    return 0


def cmd_market(args: argparse.Namespace) -> int:
    from . import marketplace as mk
    action = getattr(args, "market_action", None) or "search"
    if action == "publish":
        res = mk.publish(args.source, name=args.name or "",
                        version=args.version, description=args.description or "",
                        author=args.author or "")
        if res.get("error"):
            print(res["error"])
            return 1
        print(f"published {res['published']} v{res['version']} (grade {res['grade']})")
        return 0
    if action == "install":
        res = mk.install(args.name, enable=args.enable)
        if res.get("error"):
            print(res["error"])
            return 1
        print(f"installed {res['installed']} (grade {res['grade']}, "
              f"enabled={res['enabled']})")
        if not res["enabled"]:
            print(f"enable it: praxis plugins enable {res['installed']}")
        return 0
    if action == "uninstall":
        res = mk.uninstall(args.name)
        print(f"uninstalled {res['uninstalled']} (removed={res['removed']})")
        return 0
    # search (default)
    listings = mk.search(getattr(args, "query", "") or "")
    if not listings:
        print(f"no published plugins in {mk.registry_dir()}")
        return 0
    for item in listings:
        print(f"{item.name} v{item.version} [{item.grade}] — "
              f"{item.description or '(no description)'}"
              + (f"  by {item.author}" if item.author else ""))
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    from . import plugins as pl
    action = getattr(args, "plugins_action", None) or "list"
    if action in ("enable", "disable"):
        res = pl.set_enabled(args.name, action == "enable")
        print(f"{action}d plugin '{res['plugin']}'")
        return 0
    infos = pl.list_plugins()
    if not infos:
        print(f"no plugins found in {pl.plugins_dir()} "
              "(drop a *.py with a register(registry) function)")
        return 0
    for i in infos:
        print(f"{'[on] ' if i.enabled else '[off]'} {i.name}  ({i.path})")
    return 0


def cmd_secrets_bundle(args: argparse.Namespace) -> int:
    from .vault import CredentialVault
    v = CredentialVault()
    action = getattr(args, "bundle_action", None) or "list"
    if action == "put":
        values = {}
        for pair in args.values or []:
            if "=" in pair:
                k, _, val = pair.partition("=")
                values[k.strip()] = val
        if not values:
            print("provide values as KEY=VALUE (e.g. GITHUB_TOKEN=ghp_...)")
            return 1
        scope = (args.scope or "").split(",") if args.scope else []
        scope = [s.strip() for s in scope if s.strip()]
        b = v.put(args.name, values, scope=scope or None)
        print(f"stored bundle '{b.name}' keys={b.keys} scope={b.scope or 'all'}")
        return 0
    if action == "remove":
        print("removed" if v.delete(args.name) else "not found")
        return 0
    bundles = v.list()
    if not bundles:
        print("no credential bundles (add one: praxis secrets-bundle put NAME KEY=VAL)")
        return 0
    for b in bundles:
        print(f"{b.name}: keys={b.keys} scope={b.scope or 'all tools'}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import json as _json

    from .benchmark import run_reliability
    rep = run_reliability(k=args.k, category=args.category)
    if getattr(args, "json", False):
        print(_json.dumps(rep.to_dict(), indent=2))
    else:
        print(rep.summary())
        if rep.flaky_cases:
            print("flaky cases (nondeterminism — investigate):")
            for cid, c in rep.flaky_cases.items():
                print(f"  {cid}: passed {c}/{rep.k}")
    return 0 if rep.stable else 1


def cmd_eval(args: argparse.Namespace) -> int:
    import json
    import os

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

    if not getattr(args, "real", False):
        # The eval suite is deterministic + offline by design; force the mock
        # backends so a configured-but-unreachable provider can't make it hang.
        os.environ["PRAXIS_LLM"] = "mock"
        os.environ["PRAXIS_EMBED"] = "mock"
        os.environ["PRAXIS_MM"] = "mock"
    report = run_evals(category=args.category,
                       timeout=getattr(args, "timeout", 20.0))
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

def cmd_consolidation(args: argparse.Namespace) -> int:
    """``praxis consolidation status|run|enable|disable`` — operate the
    active memory consolidation feature (v0.28.0+). See praxis-consolidation-
    phase-plan.md."""
    action = args.action or "status"
    from .config import get_consolidation_config, set_consolidation_config

    if action == "enable":
        cc = get_consolidation_config()
        cc["enabled"] = True
        set_consolidation_config(cc)
        print("consolidation enabled (agents.consolidation.enabled=true)")
        print("the daemon will run a pass on the next interval tick; ")
        print("use 'praxis consolidation run' to trigger one now")
        return 0

    if action == "disable":
        cc = get_consolidation_config()
        cc["enabled"] = False
        set_consolidation_config(cc)
        print("consolidation disabled (agents.consolidation.enabled=false)")
        print("an in-flight pass finishes; no new passes will start")
        return 0

    # status and run both need the daemon HTTP API
    from .daemon import daemon_status
    status = daemon_status()
    if not status.get("running"):
        print("daemon not running (start with 'praxis daemon start')")
        if action == "status":
            # still show the config even when the daemon is down
            cc = get_consolidation_config()
            print(json.dumps(cc, indent=2, default=str))
        return 1
    port = status.get("port")
    if port is None:
        print("could not determine daemon port")
        return 1
    import urllib.request
    if action == "status":
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/consolidation", timeout=5
            ) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"could not fetch consolidation status: {exc}")
            return 1
        print(json.dumps(body, indent=2, default=str))
        return 0
    if action == "run":
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/consolidation/run",
                data=b"{}", headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"could not trigger consolidation: {exc}")
            return 1
        if body.get("error"):
            print(f"consolidation: {body['error']}")
            return 1
        print(json.dumps(body, indent=2, default=str))
        return 0
    print(f"unknown action: {action}")
    return 2




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

def _need_api_key(provider, api_key: str | None, action: str = "use") -> str | None:
    """Return an error message if a provider requires a key but none was supplied,
    otherwise None."""
    if not provider.needs_key:
        return None
    if api_key:
        return None
    env_name = provider.key_env or "API_KEY"
    return (f"Provider '{provider.id}' requires an API key to {action}. "
            f"Pass --api-key or set the {env_name} environment variable.")


def cmd_onboard(args: argparse.Namespace) -> int:
    if args.provider and args.model:
        prov = CATALOG[args.provider]
        api_key = args.api_key or os.environ.get(prov.key_env or "")
        err = _need_api_key(prov, api_key, action="configure")
        if err:
            print(f"Error: {err}")
            return 1
        summary = onboard_mod.run_noninteractive(
            args.provider, args.model, base_url=args.base_url,
            api_key=api_key, use_env_ref=not args.api_key,
        )
        print(f"Configured (non-interactive): model = {summary['model']}")
        print(f"Config written to: {cfg.config_path()}")
        return 0
    if args.provider:
        # Non-interactive provider given but no model: list available models
        # so the user can pick, then exit. Useful for Ollama cloud/local.
        prov = CATALOG[args.provider]
        api_key = args.api_key or os.environ.get(prov.key_env or "")
        models = discover_ollama_models(
            args.base_url or prov.base_url, api_key=api_key)
        print(f"Models available for {args.provider}:")
        for m in models or prov.suggested_models:
            print(f"  {m}")
        return 0
    onboard_mod.run()
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    """Quickly view or set the active provider/model."""
    current = cfg.get_default_model()
    if args.action == "get":
        print(current or "not configured")
        return 0
    if args.action == "set":
        provider_id, model = cfg.split_model_ref(args.model_ref)
        if not model or provider_id not in CATALOG:
            print(f"Invalid model ref: {args.model_ref}  (expected provider/model)")
            return 1
        prov = CATALOG[provider_id]
        api_key = args.api_key or os.environ.get(prov.key_env or "")
        err = _need_api_key(prov, api_key, action="set")
        if err:
            print(f"Error: {err}")
            return 1
        summary = onboard_mod.run_noninteractive(
            provider_id, model, base_url=args.base_url,
            api_key=api_key, use_env_ref=not args.api_key,
        )
        print(f"Set model: {summary['model']}")
        print(f"Config written to: {cfg.config_path()}")
        return 0
    if args.action == "list":
        for pid in ORDER:
            prov = CATALOG[pid]
            mark = "*" if current and current.startswith(pid + "/") else " "
            print(f"{mark} {pid}: {prov.label}")
            if args.discover and pid in ("ollama", "ollama-cloud"):
                api_key = None
                if pid == "ollama-cloud":
                    api_key = os.environ.get(prov.key_env or "")
                    if not api_key:
                        print("      (set OLLAMA_API_TOKEN to discover cloud models)")
                        continue
                models = discover_ollama_models(prov.base_url, api_key=api_key)
                for m in models or prov.suggested_models:
                    print(f"      - {m}")
        return 0
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    from . import demo
    demo.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    parser = argparse.ArgumentParser(prog="praxis", description="Praxis hybrid-agent CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ph = sub.add_parser("handle", help="run one full agent cycle for a goal")
    ph.add_argument("goal", help="the goal text")
    ph.add_argument("--approve-all", action="store_true",
                    help="auto-approve held consequential actions (dev only)")
    ph.add_argument("--m365", action="store_true",
                    help="use live M365 tools via the broker instead of mock tools")
    ph.set_defaults(func=cmd_handle)

    pg = sub.add_parser("goal",
                        help="Level 1 autonomous loop: run the agent until the "
                             "verifier confirms the goal is met or the turn "
                             "budget is spent (H10)")
    pg.add_argument("goal", help="the goal text to loop on")
    pg.add_argument("--max-turns", type=int, default=8,
                   help="hard cap on loop iterations (default 8)")
    pg.add_argument("--threshold", type=float, default=0.3,
                   help="verifier score in [0,1] at which the goal is met "
                        "(default 0.3, H05-calibrated)")
    pg.add_argument("--approve-all", action="store_true",
                    help="auto-approve held consequential actions each turn "
                         "(dev only)")
    pg.add_argument("--m365", action="store_true",
                    help="use live M365 tools via the broker instead of mock tools")
    pg.add_argument("--json", action="store_true",
                    help="emit the full goal record as JSON (anti-"
                         "comprehension-rot log)")
    pg.set_defaults(func=cmd_goal)

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

    pdoc = sub.add_parser("doctor",
                          help="first-run readiness checklist (model/memory/search/wiki)")
    pdoc.set_defaults(func=cmd_doctor)

    pmsg = sub.add_parser("message", help="send a message via a gateway (telegram/slack/discord/webhook/ntfy)")
    pmsg.add_argument("target", nargs="?", default="",
                      help="'<channel>' or '<channel>:<destination>'")
    pmsg.add_argument("text", nargs="?", default="", help="message body")
    pmsg.add_argument("--list", action="store_true",
                      help="list available + configured channels")
    pmsg.set_defaults(func=cmd_message)

    pevo = sub.add_parser("evolve", help="propose evolutionary skill improvements (PR-gated)")
    pevo.add_argument("skill", nargs="?", default="", help="skill name (omit = all)")
    pevo.add_argument("--apply", action="store_true",
                      help="apply after reviewing the diff (default: propose-only)")
    pevo.add_argument("--llm", action="store_true",
                      help="use the configured LLM for reflective mutation")
    pevo.set_defaults(func=cmd_evolve)

    pmk = sub.add_parser("market", help="plugin marketplace: publish/search/install")
    mksub = pmk.add_subparsers(dest="market_action")
    mks = mksub.add_parser("search", help="search published plugins (default)")
    mks.add_argument("query", nargs="?", default="")
    mkp = mksub.add_parser("publish", help="publish a plugin module")
    mkp.add_argument("source", help="path to the plugin .py")
    mkp.add_argument("--name", default="")
    mkp.add_argument("--version", default="0.1.0")
    mkp.add_argument("--description", default="")
    mkp.add_argument("--author", default="")
    mki = mksub.add_parser("install", help="install a published plugin")
    mki.add_argument("name")
    mki.add_argument("--enable", action="store_true", help="enable after install")
    mku = mksub.add_parser("uninstall", help="remove an installed plugin")
    mku.add_argument("name")
    pmk.set_defaults(func=cmd_market)

    ppl = sub.add_parser("plugins", help="manage third-party plugins")
    plsub = ppl.add_subparsers(dest="plugins_action")
    plsub.add_parser("list", help="list discovered plugins (default)")
    ple = plsub.add_parser("enable", help="enable a plugin")
    ple.add_argument("name")
    pld = plsub.add_parser("disable", help="disable a plugin")
    pld.add_argument("name")
    ppl.set_defaults(func=cmd_plugins)

    psb = sub.add_parser("secrets-bundle", help="manage named credential bundles")
    sbsub = psb.add_subparsers(dest="bundle_action")
    sbsub.add_parser("list", help="list credential bundles (default)")
    sbp = sbsub.add_parser("put", help="create/replace a bundle")
    sbp.add_argument("name")
    sbp.add_argument("values", nargs="*", help="KEY=VALUE pairs")
    sbp.add_argument("--scope", default="", help="comma-separated tool names (default: all)")
    sbr = sbsub.add_parser("remove", help="delete a bundle")
    sbr.add_argument("name")
    psb.set_defaults(func=cmd_secrets_bundle)

    pbench = sub.add_parser("bench", help="reliability benchmark (run eval suite k times)")
    pbench.add_argument("-k", type=int, default=5, help="number of runs (default 5)")
    pbench.add_argument("--category", default=None, help="restrict to one eval category")
    pbench.add_argument("--json", action="store_true", help="emit JSON")
    pbench.set_defaults(func=cmd_bench)

    pscan = sub.add_parser("scan", help="security-scan skills, MCP tools, or dependencies")
    scansub = pscan.add_subparsers(dest="scan_target")
    scansub.add_parser("skills", help="scan all installed skills for dangerous content")
    psm = scansub.add_parser("mcp", help="scan a configured MCP server's tool defs")
    psm.add_argument("--server", required=True, help="configured MCP server name")
    scansub.add_parser("deps", help="check installed deps against OSV.dev")
    pscan.set_defaults(func=cmd_scan)

    pcron = sub.add_parser("cron", help="schedule recurring autonomous jobs")
    cronsub = pcron.add_subparsers(dest="cron_action")
    pca = cronsub.add_parser("add", help="add a scheduled job")
    pca.add_argument("goal", help="the goal/prompt to run on schedule")
    pca.add_argument("--schedule", required=True,
                     help="e.g. '30m', 'daily@09:00', '0 9 * * *', 'hourly'")
    pca.add_argument("--name", default="", help="optional human label")
    pca.add_argument("--mode", default="do",
                     choices=["do", "ask", "research", "agent"],
                     help="how to run the goal (default: do = queue a task)")
    pca.add_argument("--deliver", default="local",
                     help="where to send results: 'local' or a gateway target")
    pcr = cronsub.add_parser("remove", help="delete a job")
    pcr.add_argument("job_id")
    pcp = cronsub.add_parser("pause", help="pause a job")
    pcp.add_argument("job_id")
    pcre = cronsub.add_parser("resume", help="resume a paused job")
    pcre.add_argument("job_id")
    cronsub.add_parser("list", help="list cron jobs (default)")
    pcron.set_defaults(func=cmd_cron)

    pjobs = sub.add_parser("jobs", help="first-class vertical jobs (research/draft/schedule)")
    jsub = pjobs.add_subparsers(dest="jobs_action")
    jsub.add_parser("list", help="list jobs (default)")
    jrun = jsub.add_parser("run", help="run a job")
    jrun.add_argument("job", choices=["research", "draft", "schedule"])
    jrun.add_argument("--query", default="", help="research query")
    jrun.add_argument("--prompt", default="", help="draft prompt")
    jrun.add_argument("--goal", default="", help="schedule goal")
    jrun.add_argument("--schedule", default="0 9 * * 1-5", help="cron schedule")
    jrun.add_argument("--name", default="colleague", help="cron job name")
    pjobs.set_defaults(func=cmd_jobs)

    pbud = sub.add_parser("budget", help="spend budget hard-stop status/set/reset")
    bsub = pbud.add_subparsers(dest="budget_action")
    bsub.add_parser("status", help="show budget (default)")
    bset = bsub.add_parser("set", help="set USD cap")
    bset.add_argument("limit", type=float)
    bsub.add_parser("reset", help="zero spent counter")
    pbud.set_defaults(func=cmd_budget)

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

    ppe = sub.add_parser(
        "plan-run", help="decompose a goal into governed steps and execute them")
    ppe.add_argument("goal", help="the goal to plan and execute")
    ppe.add_argument("--max-replans", type=int, default=1,
                     help="how many times a failed step may be replanned (default 1)")
    ppe.add_argument("--m365", action="store_true", help="use the M365 toolset")
    ppe.set_defaults(func=cmd_plan_run)

    pth = sub.add_parser(
        "think", help="deep-think: multi-round deliberation on a hard question")
    pth.add_argument("question", help="the question to deliberate on")
    pth.add_argument("--rounds", type=int, default=2,
                     help="max debate rounds when there is no consensus (default 2)")
    pth.add_argument("--force", action="store_true",
                     help="deliberate even if the question isn't classified hard")
    pth.set_defaults(func=cmd_think)

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
                          "(tool_use, approval, safety, schema, vertical, ...)")
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
    pev.add_argument("--real", action="store_true",
                     help="evaluate against the configured provider instead of the "
                          "deterministic offline mock (may be slow / need a network)")
    pev.add_argument("--timeout", type=float, default=20.0, metavar="SECS",
                     help="per-case timeout (0 disables); a case that exceeds it is "
                          "failed rather than hanging the suite")
    pev.set_defaults(func=cmd_eval)

    pmp = sub.add_parser("memory-purge",
                         help="purge expired/old memory by retention policy")
    pmp.add_argument("--decay-days", type=float, default=None,
                     help="forget episodic items older than N days under salience floor")
    pmp.add_argument("--salience-floor", type=float, default=0.2)
    pmp.add_argument("--forget-provenance", default=None,
                     help="bulk-delete items whose provenance starts with this prefix")
    pmp.set_defaults(func=cmd_memory_purge)

    pcn = sub.add_parser("consolidation",
                         help="active memory consolidation (status/run/enable/disable)")
    pcn.add_argument("action", nargs="?", default="status",
                     choices=["status", "run", "enable", "disable"],
                     help="status = show config + pending; run = trigger a pass; "
                          "enable/disable = flip agents.consolidation.enabled")
    pcn.set_defaults(func=cmd_consolidation)


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

    po = sub.add_parser("onboard", help="pick a model provider + model (interactive or --provider/--model)")
    po.add_argument("--provider", choices=list(ORDER),
                    help="non-interactive: provider id (e.g. ollama, ollama-cloud, openai)")
    po.add_argument("--model", help="non-interactive: model id")
    po.add_argument("--base-url", default=None, help="non-interactive: custom base URL")
    po.add_argument("--api-key", default=None,
                    help="non-interactive: paste key (else an env reference is used)")
    po.set_defaults(func=cmd_onboard)

    pmdl = sub.add_parser("model", help="quickly view/list/set the active provider/model")
    mdl_sub = pmdl.add_subparsers(dest="action", required=True)
    mdl_get = mdl_sub.add_parser("get", help="show the currently configured model ref")
    mdl_get.set_defaults(func=cmd_model)
    mdl_set = mdl_sub.add_parser("set", help="set the active model (provider/model)")
    mdl_set.add_argument("model_ref", help="model ref to use, e.g. openai/gpt-4o-mini")
    mdl_set.add_argument("--base-url", default=None, help="override provider base URL")
    mdl_set.add_argument("--api-key", default=None,
                         help="paste key now (else env reference is used)")
    mdl_set.set_defaults(func=cmd_model)
    mdl_list = mdl_sub.add_parser("list", help="list all providers; --discover for Ollama models")
    mdl_list.add_argument("--discover", action="store_true",
                          help="probe ollama/ollama-cloud for available models")
    mdl_list.set_defaults(func=cmd_model)

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
    pmcp.add_argument("--list-presets", action="store_true",
                      help="list prebuilt MCP server presets (e.g. xai-docs)")
    pmcp.add_argument("--enable-preset", metavar="NAME",
                      help="enable a prebuilt MCP preset (e.g. xai-docs) in config")
    pmcp.set_defaults(func=cmd_mcp)

    pdm = sub.add_parser("daemon", help="long-running task worker")
    pdm.add_argument("action", nargs="?", choices=["start", "stop", "status", "logs", "submit"],
                     help="daemon action")
    pdm.add_argument("--port", type=int, default=None, help="control HTTP port")
    pdm.add_argument("--host", default=None,
                     help="bind host (default 127.0.0.1; set 0.0.0.0 or PRAXIS_HOST in containers)")
    pdm.add_argument("--work-dir", default=None, help="working directory")
    pdm.add_argument("--goal", default="", help="goal to submit (with submit action)")
    pdm.add_argument("--max-attempts", type=int, default=3)
    pdm.add_argument("--lines", type=int, default=100, help="log lines to fetch")
    pdm.set_defaults(func=cmd_daemon)

    pgov = sub.add_parser(
        "governance", help="view or set the governance compliance mode")
    pgov.add_argument(
        "action", nargs="?",
        choices=["status", "enforced", "autonomous", "permissive"],
        help="show status, or set the mode (enforced/autonomous/permissive)")
    pgov.add_argument(
        "--for", dest="for_", default=None,
        help="auto-revert to enforced after this duration (e.g. 30m, 1h, 90s)")
    pgov.set_defaults(func=cmd_governance)

    pup = sub.add_parser("update",
                         help="upgrade the installed praxis-agent + migrate config")
    pup.add_argument("--check", action="store_true",
                     help="only check for a newer version, don't install")
    pup.set_defaults(func=cmd_update)

    psec = sub.add_parser("secrets",
                          help="manage stored provider API keys (keychain/file)")
    psec.add_argument("action", nargs="?",
                      choices=["status", "set", "rm", "migrate"],
                      help="status (default), set, rm, or migrate to the OS keychain")
    psec.add_argument("--provider", default=None, help="provider id (for set/rm)")
    psec.set_defaults(func=cmd_secrets)

    ppk = sub.add_parser(
        "pack", help="vertical packs: bundle prompt + policy + tools for a domain")
    ppk.add_argument(
        "action", nargs="?",
        choices=["list", "show", "create", "install", "activate", "deactivate",
                 "templates"],
        help="list (default), show, create, install, activate, deactivate, templates")
    ppk.add_argument("name", nargs="?", help="pack name (or a directory path for install)")
    ppk.add_argument("--vertical", default=None, help="vertical label (for create)")
    ppk.set_defaults(func=cmd_pack)

    return parser


def _maybe_first_run_onboard(command: str) -> None:
    """Offer onboarding on first use when nothing is configured (TTY only)."""
    if command in ("onboard", "demo", "eval", "tui", "m365", "mcp", "daemon",
                   "approvals", "approve", "ingest", "recall", "describe", "route", "ask",
                   "learn", "skills", "skill", "compliance", "governance", "update",
                   "secrets", "pack",
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
    try:
        cfg.migrate_config()
    except Exception:
        pass
    _maybe_first_run_onboard(args.command)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
