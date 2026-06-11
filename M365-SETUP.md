# Connecting Praxis to Microsoft 365

Praxis acts on your calendar, mail, and files **only through the OpenClaw M365
Access Broker** — a separate local control plane that gates every Microsoft
Graph call (auth, least-privilege scopes, tool allowlist, approval gates,
injection firewall, redacted hash-chained audit log).

This works against **any tenant you control** — including your **personal M365 /
Entra tenant**. Nothing here touches a work environment.

```
Praxis (governance + memory) ──HTTP──> M365 Broker ──MSAL+Graph──> Microsoft 365
        autonomy for prep                approval gate, audit          (your tenant)
        approval for consequence         injection firewall
```

## 1. Get the broker

The broker lives in `openclaw-m365-broker/` (Node ≥ 20, zero runtime deps in
dry-run).

```bash
cd openclaw-m365-broker
npm test          # optional: 79 tests, node:test
npm start         # serves http://127.0.0.1:8787 (dry-run mock by default)
```

On Windows PowerShell with script execution disabled, run the server directly:

```powershell
node src/server.js
```

Set persistent keys so Praxis can authenticate (otherwise ephemeral keys are
printed at startup):

```bash
# two SEPARATE keys — the agent can never mint its own approval
setx BROKER_KEY          "<long-random-agent-key>"
setx BROKER_APPROVER_KEY "<long-random-approver-key>"
```

## 2. Point Praxis at the broker

```bash
setx M365_BROKER_URL          "http://127.0.0.1:8787"
setx M365_BROKER_KEY          "<same as BROKER_KEY>"
setx M365_BROKER_APPROVER_KEY "<same as BROKER_APPROVER_KEY>"
```

Verify the connection:

```bash
praxis m365
# -> broker health: { ok: true, mode: dry-run, requiredScopes: [...] }
# -> status: signed-in user (mock in dry-run)
```

Run a cycle against the broker (reads/draft autonomous; send/share/delete held):

```bash
praxis handle "Prepare a customer follow-up email and send it" --m365
# review the held action, then approve it from the prompt (or --approve-all in dev)
```

## 3. Go live against your tenant

In dry-run the broker returns mock data — perfect for testing the wiring. To use
real Graph against **your personal tenant**:

1. **Register a single-tenant Entra app** in *your* tenant (delegated auth, public
   client / PKCE). A free **Microsoft 365 Developer tenant** works.
2. Grant exactly the **least-privilege scopes** the broker prints at startup and at
   `GET /health` — start read-only: `User.Read`, `Calendars.Read`, `Mail.Read`,
   `Files.Read`. Add write/send scopes only after the read paths work.
3. In `openclaw-m365-broker/`: `cp .env.example .env`, set `BROKER_DRY_RUN=false`,
   `MS_TENANT_ID`, `MS_CLIENT_ID`, then `npm install @azure/msal-node` (loaded
   lazily; not needed for dry-run).
4. Restart the broker and re-run `praxis m365` — `mode` should no longer be
   `dry-run`.

## Safety model (unchanged end-to-end)

- **Reads & drafts** run autonomously (broker `read`/`write` classes → Praxis
  `READ`/`DRAFT`).
- **Send / share / delete** are **held** by Praxis. On your approval, Praxis (as
  the host UI) calls the broker's `/approve` with the **approver key** to mint a
  single-use, tool-scoped token, then `/execute`. The agent key alone can never
  send, share, or delete.
- **Retrieved content is evidence, not instruction** — the broker's injection
  firewall scans every external read; high-risk content is quarantined and
  surfaced in the action log (`[firewall:high …]`), never executed.
- **Everything is audited** — redacted, hash-chained `audit.log` in the broker.
