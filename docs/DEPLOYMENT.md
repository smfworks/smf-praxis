# Deployment — Local vs Shared / Network

Praxis runs the same code in three shapes. Pick by **who reaches the dashboard**:

| Mode | Who connects | Bind | Auth |
|---|---|---|---|
| **Local** (default) | just you, this machine | `127.0.0.1:8643` | n/a (loopback) |
| **Docker** | just you, via container | `127.0.0.1:8643` (loopback-mapped) | n/a |
| **Shared / LAN** | a team, over the network | `0.0.0.0:8643` | ⚠️ **you must add it** |

> ⚠️ **The dashboard ships with no built-in auth.** Loopback (local/Docker) is safe.
> The moment you bind a routable address you are exposing an ungoverned control
> plane — always front it with a reverse proxy, VPN, or SSH tunnel (see §3).

---

## 1. Local (single user)

```bash
pipx install praxis-agent            # or: pip install praxis-agent
praxis daemon start                  # Command Deck → http://127.0.0.1:8643
praxis daemon status | stop
```

- Binds **127.0.0.1** only; nothing leaves the machine.
- State lives in `~/.praxis/` (SQLite store, knowledge base, config, packs). Override
  with `PRAXIS_HOME=/path`. Port defaults to **8643** and auto-increments if taken.
- Keys: env-var reference by default, OS keychain with `[keyring]`, else gitignored file.

## 2. Docker (single host)

```bash
docker compose up        # http://127.0.0.1:8643
```

- Binds `0.0.0.0` *inside* the container, mapped to host **loopback only**
  (`127.0.0.1:8643:8643`). `/data` volume holds the store/KB/config. Keys via env
  (`OPENAI_API_KEY`, …); none ⇒ offline mock LLM. Runs as non-root, `restart: unless-stopped`.

## 3. Shared / network access (a team)

Two parts: **bind a routable address**, then **put auth in front**.

```bash
praxis daemon start --host 0.0.0.0 --port 8643   # or set PRAXIS_HOST=0.0.0.0
```

Compose: change the map to `8643:8643` (drop the `127.0.0.1:` prefix). **Do not stop
there** — add a front door:

```nginx
# nginx: TLS + basic-auth in front of the loopback daemon
server {
  listen 443 ssl;
  location / { auth_basic "Praxis"; auth_basic_user_file /etc/nginx/.htpasswd;
               proxy_pass http://127.0.0.1:8643; }
}
```

Keep `--host 127.0.0.1` and let only the proxy reach it. Lighter options: a
**VPN/Tailscale** subnet, or per-user `ssh -L 8643:127.0.0.1:8643 host`.

- **Data:** one daemon + one `~/.praxis` store. SQLite is lock-guarded for concurrent
  runs but is **not** a multi-tenant DB — give teams a shared *front end*, not parallel
  daemons on the same dir. Per-user isolation = separate `PRAXIS_HOME` + port.
- **Firewall:** only expose the proxy's 443; never 8643 publicly.

## 4. Checklist & verify

- [ ] Loopback for local/Docker; reverse-proxy + TLS + auth before any LAN bind
- [ ] `PRAXIS_HOME` set for shared/persistent data; back it up
- [ ] Keys via env/keychain, never committed
- [ ] `curl -s localhost:8643/status` returns JSON; `praxis update` to upgrade

Roadmap: built-in dashboard auth (post-p12) will fold §3's front door inward.
