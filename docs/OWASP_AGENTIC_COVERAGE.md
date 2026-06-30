# Praxis — OWASP Agentic Top-10 Coverage Matrix

Maps the OWASP Agentic Security Initiative's Top-10 agentic-AI threats to the
concrete Praxis controls that mitigate them. This is a living attestation: every
row names the module/mechanism so claims are auditable against code.

Praxis version: see `hybridagent.__version__`. Controls below are **built-in**
(dependency-free core) unless marked *(extra)*.

| # | OWASP Agentic Threat | Praxis control(s) | Module |
|---|---|---|---|
| **AAI001** | **Memory Poisoning** — corrupting agent memory/RAG to steer future behavior | Contradiction detection on ingest; cite-or-abstain grounding; provenance on memory items; skill security scan blocks malicious skill bodies before they enter procedural memory | `contradiction.py`, `grounding.py`, `memory.py`, `security_scan.py` |
| **AAI002** | **Tool Misuse** — agent tricked into harmful tool calls | Risk-classed tools (READ/DRAFT autonomous, SEND/DESTRUCTIVE held); JSON-schema validation before the broker; per-tool allowlist; dual-approval for destructive | `broker.py`, `tools.py`, `validation.py` |
| **AAI003** | **Privilege Compromise** — abusing excess permissions | Least-privilege role tool-allowlists for subagents; pack tool restriction; kill-switch on consequential actions; (G6) sandboxed execution backend | `orchestrator.py`, `broker.py`, `sandbox.py` |
| **AAI004** | **Resource Overload** — exhausting compute/cost/rate | Enforceable token/cost budget; iteration caps; subagent recursion depth cap (MAX_DEPTH); per-run routing+cost accounting | `broker.py`/budget, `orchestrator.py`, `pricing.py`, `persistence.py` |
| **AAI005** | **Cascading Hallucination** — false outputs propagating | Verifier critic gate; reflexion self-review; cite-or-abstain; multi-agent debate for high-stakes | `verifier.py`, `reflexion.py`, `grounding.py`, `debate.py` |
| **AAI006** | **Intent Breaking & Goal Manipulation** — hijacking the agent's objective | Prompt-injection detection; untrusted-content quarantine (data-boundary wrapping); injection-flagged goals pinned to least-privilege role | `broker.py` (`is_injection`), `content_guard.py`, `orchestrator.py` |
| **AAI007** | **Misaligned/Deceptive Behaviors** — agent acts against user interest | Full audit trail of every decision; signed attestations (tamper-evident attribution); recall-preview surfaces what memory/skills entered each turn | `broker.py` audit, `identity.py`, `daemon.py` (`_recall_preview`) |
| **AAI008** | **Repudiation & Untraceability** — can't prove who did what | Persistent audit entries + compliance events; per-agent cryptographic identity + signed attestations; durable replayable run traces | `identity.py`, `persistence.py`, `daemon.py` runs |
| **AAI009** | **Identity Spoofing & Impersonation** — faking agent/user identity | Per-agent Ed25519/HMAC identity with verifiable attestations; A2A peers carry auth headers; DM/peer allowlists | `identity.py`, `a2a_client.py`, `gateways.py` |
| **AAI010** | **Overwhelming Human-in-the-Loop** — approval fatigue / bypass | Approval idempotency (dedup identical actions); risk-tiered approvals (only consequential held); compliance modes with timed auto-revert; egress firewall reduces noise by blocking exfiltration outright | `broker.py` |

## Defense-in-depth summary

Praxis layers controls so no single bypass is catastrophic:

1. **Input boundary** — injection detection + untrusted-content quarantine (AAI006)
2. **Supply chain** — skill/MCP security scanning + OSV deps (AAI001, AAI002)
3. **Decision chokepoint** — the governance broker authorizes *every* tool call;
   optional external policy hook (OPA/Rego/Cedar) for deterministic org policy
   (AAI002, AAI003)
4. **Execution isolation** — sandboxed backend for shell/code *(G6)* (AAI003)
5. **Output verification** — verifier + reflexion + cite-or-abstain (AAI005)
6. **Attribution** — signed attestations + audit trail + run traces (AAI007–009)
7. **Cost/scope limits** — budgets, recursion caps, rate (AAI004)

## External policy hook (deterministic org policy)

`GovernancePolicy.policy_hook` accepts any callable
`hook(ctx) -> "deny" | "allow" | None`, evaluated **first** in `authorize()`:

- `"deny"` is an absolute veto (defense in depth over built-in logic)
- `"allow"` short-circuits the consequential path for an explicitly whitelisted action
- `None` defers to built-in governance
- a **raising** hook fails **safe** (treated as deny) — a broken policy can never widen access

This is the integration point for OPA/Rego, AWS Cedar, or a custom rules engine
without modifying broker internals, keeping the dependency-free core intact.

## Honest limitations

- Default identity backend is **HMAC-SHA256** (stdlib); asymmetric **Ed25519**
  (public verification, true cross-party zero-trust) activates automatically when
  the optional `cryptography` package is installed.
- The sandbox backend *(G6)* provides process/filesystem/network isolation via
  Docker when available; the pure-local backend is path-confinement only.
- This matrix is a **control inventory**, not a certification. It documents
  mitigations, not a guarantee of completeness.
