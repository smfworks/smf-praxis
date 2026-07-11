# Loop Engineering — Maturity Ladder (H10)

> Source: [Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-13-loop-engineering/) Lecture 13, and the vault note `LLM-as-a-Verifier — General-Purpose Verification Framework`.
> Status: **Level 1 (Goal Runner) shipped** in v0.21.11 — `praxis goal` runs the agent until the independent verifier (H05) confirms the goal is met or the turn budget is spent.

## The six loop primitives — Praxis mapping

The harness course (Osmani) decomposes a loop into six primitives. Praxis already has all six; H10's job is to wire them deliberately into a maturity progression rather than leaving them as disconnected capabilities.

| Primitive | What it is | Praxis module | Maturity |
|---|---|---|---|
| **Automations** | Scheduled triggers (the heartbeat) | `cli.py` cron + `daemon.py` + `task_manager.py` | Level 2 ready |
| **Worktrees** | Parallel task isolation | `orchestrator.py` (scoped subagents) | Level 3 ready |
| **Skills** | Codified project knowledge | `skills.py` + `skill_evaluator.py` + `evolution.py` | active |
| **Connectors** | External tool access (MCP/A2A/gateways) | `mcp_client.py` + `mcp_adapter.py` + `gateways.py` + `real_tools.call_agent` | active |
| **Sub-agents** | Maker/checker split | `orchestrator.py` + `verifier.py` (H05) | active |
| **External State** | Cross-iteration memory | `persistence.py` + `memory.py` + `rag.py` | active |

## The maturity ladder

| Level | What it is | Praxis status |
|---|---|---|
| **0: Manual one-shot** | `praxis handle "<goal>"` — one cycle, you re-trigger each time | was the baseline |
| **1: Goal Runner** | `praxis goal "<goal>"` — loops until the verifier confirms done or the turn budget is spent | **shipped v0.21.11** |
| **2: Scheduled Single-Task** | One automation runs one task on a timer (morning CI check, daily triage) | ready: `praxis cron` + `praxis goal` |
| **3: Multi-Agent Loop** | Maker/checker split; each finding forks an isolated subagent | `orchestrator.py` has scoped subagents; needs PPT best-of-N (H05 deferred path) |
| **4: Self-Feeding Loop** | The loop auto-discovers its next task from external state (issue tracker, feature_list.json) | future |
| **5: Fleet Orchestration** | Multiple loops run in parallel, independent but sharing a memory layer | future |

## Level 1 — what shipped (v0.21.11)

`hybridagent/goal_runner.py` — `GoalRunner` class. `praxis goal "<goal>"` CLI command.

The loop:
1. Call `agent.handle(goal)` — one perceive→plan→govern→act→consolidate cycle.
2. Score the answer with the H05 verifier (continuous reward when an LLM critic is configured; deterministic binary verdict otherwise).
3. Stop when `verifier.approved and progress >= threshold` (default 0.3, H05-calibrated), or when a held action blocks, or when `max_turns` is hit.
4. Surface each turn's progress to the operator (fights cognitive surrender).
5. Emit a JSON record (`--json`) so the operator can read what happened without re-reading every turn (fights comprehension rot).

### The four silent costs — guarded by construction

| Cost | How H10 guards against it |
|---|---|
| **Verification debt** | The stop condition is machine-checkable (verifier score ≥ threshold), never "feels about right." The H05 verifier is the independent judge. |
| **Comprehension rot** | `to_record()` serializes every turn's summary, progress, and verdict. The operator reads this instead of the full transcript. |
| **Cognitive surrender** | Each turn's progress score is printed live. The operator stays engaged, not blind. |
| **Token blowout** | A hard `max_turns` cap (default 8) bounds the loop. Context compaction is the agent's existing `compact_tool_messages`. |

### Governance is unchanged

The loop calls the same `agent.handle` that runs read/draft tools autonomously and holds send/destructive for approval. The loop **never** auto-approves held actions unless the operator passed `--approve-all` (dev-only). A held action stops the loop with `stopped_reason="blocked"` — the operator decides.

## How to use it

```bash
# Level 1: run the agent on a goal until the verifier confirms done
praxis goal "fix the failing test in utils.py" --max-turns 5

# With the H05 LLM-verifier critic (when configured in praxis.json):
# agents.verification.critic: "llm-verifier"
# — the loop uses the continuous reward as the progress score.

# Emit the full goal record as JSON (the anti-comprehension-rot log)
praxis goal "..." --json

# Level 2 (scheduled): wire praxis goal to a cron trigger
praxis cron add --goal "check for new issues and triage" --every 30m
```

## Next levels (future work)

- **Level 2** is a thin wrapper: `praxis cron` already triggers `praxis handle` on a schedule; swapping `handle` for `goal` gives a scheduled Goal Runner.
- **Level 3** (multi-agent) needs the PPT best-of-N selection path deferred from H05 — generate N candidate trajectories, score with the verifier, pick the best. `orchestrator.py`'s scoped subagents are the isolation layer.
- **Level 4** (self-feeding) reads `feature_list.json` to discover the next `not_started` feature and loops on it autonomously. The WIP=1 architectural check (H06) keeps it disciplined.