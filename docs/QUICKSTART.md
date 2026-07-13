# 5-Minute First Trial

Goal: from nothing to a governed task on your machine — safely, loopback-only. No
key required (offline mock); add one to see a live model.

## 1. Install (1 min)

```bash
curl -fsSL https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.sh | bash
# from a clone:  ./install.sh   (mac/Linux)   .\install.ps1   (Windows)
```

The release workflow publishes versioned wheel and sdist artifacts to GitHub
Releases; PyPI publication is currently disabled.

## 2. Configure — optional (1 min)

```bash
praxis onboard                   # pick provider + model; paste a key or use an env var
```

Skip it to stay on the deterministic **mock** model — everything below still works.

## 3. Start the Command Deck (30 s)

```bash
praxis daemon start              # → http://127.0.0.1:8643  (loopback only, safe)
praxis daemon status             # confirm it's up;  praxis daemon stop  to halt
```

Open the dashboard. You're on `127.0.0.1` — nothing is exposed to the network.

## 4. Run your first governed task (1 min)

```bash
praxis ask "Summarize what Praxis can do"          # grounded Q&A, cites or abstains
praxis daemon submit --goal "Draft a follow-up email to the team"
```

Reads/drafts run automatically; **send/destructive actions are held** for your
approval on the dashboard. Approve to release — that's the whole point.

## 5. Try a vertical pack (1 min)

```bash
praxis pack activate homeschool   # persona + policy + knowledge + lesson-plan skill
praxis ask "How many instructional days do I plan for?"
praxis pack deactivate            # back to defaults
```

## Verify it's healthy

```bash
praxis eval        # expect "40/40 passed  OK"
```

## Optional: professional document output

The dependency-free install includes Artifact Studio validation, canonical
JSON/Markdown, versioning, comparison, and release-bundle verification. From a
clone, install `.[artifacts]` to add DOCX, PDF, PPTX, and XLSX renderers. The public
Python workflow is documented in [`artifacts/README.md`](artifacts/README.md).

## What to poke at (and report back)

- Approvals: does holding send/destructive feel right? ⏸️
- Packs: activate `homeschool`, see if grounding + skill help your prompts. 📦
- Notifications: set `agents.notify` to ping you on done/blocked. 📣
- File a note on anything rough — that's what the trial is for.

**Stay loopback.** Before sharing on a LAN, read [`DEPLOYMENT.md`](DEPLOYMENT.md) — the
dashboard has no auth yet, so it needs a reverse proxy/VPN first. Packs: [`PACKS.md`](PACKS.md).
