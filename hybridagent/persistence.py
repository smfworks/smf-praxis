"""Durable, on-disk state for Praxis (stdlib ``sqlite3`` — stays dependency-free).

Persists the three things that were previously lost at process exit:

* **memory** tiers (episodic / durable) — so ``praxis remember`` actually sticks
  and learned skills/facts survive restarts;
* **audit** entries — an attributable, durable trail of every governed decision;
* **approvals** — held consequential actions, so a ``send`` queued in one process
  can be reviewed and approved in another (``praxis approvals`` / ``praxis approve``).

The store is opt-in: ``Memory()``/``GovernanceBroker()`` with no store behave
exactly as before (pure in-memory), which keeps unit tests deterministic. The CLI
and TUI construct a persistent agent backed by ``~/.praxis/praxis.db``.
"""
from __future__ import annotations

import array
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config as cfg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tier       TEXT NOT NULL,
    text       TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'agent',
    kind       TEXT NOT NULL DEFAULT 'note',
    salience   REAL NOT NULL DEFAULT 1.0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_access_ts REAL,
    expires_at REAL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_tier ON memory_items(tier);

CREATE TABLE IF NOT EXISTS audit_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL DEFAULT '',
    cycle_id TEXT NOT NULL DEFAULT '',
    actor   TEXT NOT NULL,
    tool    TEXT NOT NULL,
    risk    TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail  TEXT NOT NULL,
    policy_rule TEXT NOT NULL DEFAULT '',
    approval_id TEXT NOT NULL DEFAULT '',
    args_hash TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    cycle_id    TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL DEFAULT '{}',
    preview     TEXT NOT NULL DEFAULT '',
    provenance  TEXT NOT NULL DEFAULT 'plan',
    rationale   TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    ts          REAL NOT NULL,
    expires_at  REAL,
    resolved_at REAL,
    approved_by TEXT NOT NULL DEFAULT '',
    approval_notes TEXT NOT NULL DEFAULT '',
    required_approvals INTEGER NOT NULL DEFAULT 1,
    signatures_json TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS vectors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ns         TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    text       TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'document',
    kind       TEXT NOT NULL DEFAULT 'document',
    embedding  BLOB NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vectors_ns ON vectors(ns);

CREATE TABLE IF NOT EXISTS compliance_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    ref_id     TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_compliance_cycle ON compliance_events(cycle_id);
CREATE INDEX IF NOT EXISTS ix_compliance_type ON compliance_events(event_type);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    next_retry_ts REAL,
    cycle_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    output TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS kb_sources (
    source_id TEXT PRIMARY KEY,
    uri TEXT NOT NULL,
    source_type TEXT NOT NULL,
    ns TEXT NOT NULL DEFAULT 'kb',
    title TEXT NOT NULL DEFAULT '',
    last_hash TEXT NOT NULL DEFAULT '',
    last_ingested_ts REAL,
    refresh_interval_seconds REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kb_sources_enabled ON kb_sources(enabled);
CREATE INDEX IF NOT EXISTS ix_kb_sources_ns ON kb_sources(ns);

CREATE TABLE IF NOT EXISTS skill_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    goal TEXT NOT NULL,
    outcome TEXT NOT NULL,
    score REAL NOT NULL,
    cycle_id TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_skill_outcomes_name ON skill_outcomes(skill_name);

CREATE TABLE IF NOT EXISTS skill_metadata (
    skill_name TEXT PRIMARY KEY,
    usage_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    quality_score REAL NOT NULL DEFAULT 0.0,
    last_used_ts REAL,
    quarantined INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_instances (
    agent_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'idle',
    load INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_ts REAL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_instances_role ON agent_instances(role);

CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    cycle_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_subagent_runs_agent ON subagent_runs(agent_id);
CREATE TABLE IF NOT EXISTS router_models (
    name       TEXT PRIMARY KEY,
    model_json TEXT NOT NULL,
    n_samples  INTEGER NOT NULL DEFAULT 0,
    trained_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    passes      INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_baseline (
    name        TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    goal        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'plan',
    status      TEXT NOT NULL DEFAULT 'running',
    started_ts  REAL NOT NULL,
    ended_ts    REAL,
    event_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    node_id   TEXT NOT NULL DEFAULT '',
    label     TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);
CREATE TABLE IF NOT EXISTS board_cards (
    card_id    TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    goal       TEXT NOT NULL DEFAULT '',
    lane       TEXT NOT NULL DEFAULT 'backlog',
    run_id     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS budget (
    name         TEXT PRIMARY KEY,
    limit_usd    REAL NOT NULL DEFAULT 0,
    spent_usd    REAL NOT NULL DEFAULT 0,
    runs         INTEGER NOT NULL DEFAULT 0,
    period_start REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS killswitch (
    name       TEXT PRIMARY KEY,
    engaged    INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance (
    name       TEXT PRIMARY KEY,
    mode       TEXT NOT NULL DEFAULT 'enforced',
    expires_ts REAL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_routing (
    run_id            TEXT PRIMARY KEY,
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    calls             INTEGER NOT NULL DEFAULT 0,
    local             INTEGER NOT NULL DEFAULT 0,
    fallbacks         INTEGER NOT NULL DEFAULT 0,
    escalations       INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cron_jobs (
    job_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL,
    schedule      TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'do',
    deliver       TEXT NOT NULL DEFAULT 'local',
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_run_ts   REAL,
    last_run_ts   REAL,
    last_status   TEXT NOT NULL DEFAULT '',
    last_output   TEXT NOT NULL DEFAULT '',
    runs          INTEGER NOT NULL DEFAULT 0,
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cron_enabled ON cron_jobs(enabled);
"""


class Store:
    """Thread-safe SQLite wrapper. One connection, guarded by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._vec_versions: dict[str, int] = {}
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets concurrent readers proceed alongside a single writer
            # (e.g. a heartbeat agent reading while `praxis approve` writes in a
            # second process); busy_timeout backs off instead of raising
            # "database is locked" under cross-process contention.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:  # pragma: no cover - exotic filesystems only
                pass
            self._conn.executescript(_SCHEMA)
            self._migrate_locked()
            self._conn.commit()

    def _migrate_locked(self) -> None:
        """Best-effort additive migrations for existing ~/.praxis/praxis.db files."""
        self._ensure_columns_locked("audit_entries", {
            "decision_id": "TEXT NOT NULL DEFAULT ''",
            "cycle_id": "TEXT NOT NULL DEFAULT ''",
            "policy_rule": "TEXT NOT NULL DEFAULT ''",
            "approval_id": "TEXT NOT NULL DEFAULT ''",
            "args_hash": "TEXT NOT NULL DEFAULT ''",
        })
        self._ensure_columns_locked("approvals", {
            "cycle_id": "TEXT NOT NULL DEFAULT ''",
            "decision_id": "TEXT NOT NULL DEFAULT ''",
            "rationale": "TEXT NOT NULL DEFAULT ''",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "resolved_at": "REAL",
            "approved_by": "TEXT NOT NULL DEFAULT ''",
            "approval_notes": "TEXT NOT NULL DEFAULT ''",
            "required_approvals": "INTEGER NOT NULL DEFAULT 1",
            "signatures_json": "TEXT NOT NULL DEFAULT '[]'",
        })
        self._ensure_columns_locked("memory_items", {
            "salience": "REAL NOT NULL DEFAULT 1.0",
            "access_count": "INTEGER NOT NULL DEFAULT 0",
            "last_access_ts": "REAL",
            "expires_at": "REAL",
        })
        self._ensure_columns_locked("tasks", {
            "output": "TEXT NOT NULL DEFAULT ''",
            "plan": "TEXT NOT NULL DEFAULT ''",
        })
        self._ensure_columns_locked("run_routing", {
            "escalations": "INTEGER NOT NULL DEFAULT 0",
            "escalation_reason": "TEXT NOT NULL DEFAULT ''",
        })
        self._ensure_columns_locked("compliance", {"expires_ts": "REAL"})

    def _ensure_columns_locked(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Store":
        target = Path(path) if path else cfg.home_dir() / "praxis.db"
        return cls(target)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- memory
    def add_memory(self, tier: str, text: str, provenance: str,
                   kind: str, ts: float | None = None,
                   salience: float = 1.0,
                   expires_at: float | None = None) -> int:
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_items"
                "(tier,text,provenance,kind,salience,expires_at,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (tier, text, provenance, kind, salience, expires_at, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def load_memory(self, tier: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,text,provenance,kind,salience,access_count,"
                "last_access_ts,expires_at,ts FROM memory_items "
                "WHERE tier=? ORDER BY id ASC", (tier,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_memory(self, tier: str | None = None, limit: int = 300) -> list[dict]:
        """All memory items (optionally one tier), newest first — Memory Studio."""
        cols = ("id,tier,text,provenance,kind,salience,access_count,"
                "last_access_ts,expires_at,ts")
        with self._lock:
            if tier:
                rows = self._conn.execute(
                    f"SELECT {cols} FROM memory_items WHERE tier=? "
                    "ORDER BY id DESC LIMIT ?", (tier, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {cols} FROM memory_items ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def record_memory_access(self, memory_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET access_count=access_count+1, "
                "last_access_ts=? WHERE id=?", (time.time(), memory_id),
            )
            self._conn.commit()

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memory_items WHERE id=?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- audit
    def add_audit(self, actor: str, tool: str, risk: str, verdict: str,
                  detail: str, ts: float | None = None,
                  decision_id: str = "", cycle_id: str = "",
                  policy_rule: str = "", approval_id: str = "",
                  args_hash: str = "") -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_entries"
                "(decision_id,cycle_id,actor,tool,risk,verdict,detail,"
                "policy_rule,approval_id,args_hash,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, cycle_id, actor, tool, risk, verdict, detail,
                 policy_rule, approval_id, args_hash, ts),
            )
            self._conn.commit()

    def load_audit(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision_id,cycle_id,actor,tool,risk,verdict,detail,"
                "policy_rule,approval_id,args_hash,ts FROM audit_entries "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ----------------------------------------------------------- approvals
    def upsert_approval(self, approval_id: str, tool: str, args: dict,
                        preview: str, provenance: str,
                        expires_at: float | None, cycle_id: str = "",
                        decision_id: str = "", rationale: str = "",
                        evidence: list[dict] | None = None,
                        required_approvals: int = 1) -> None:
        evidence_json = json.dumps(evidence or [])
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO approvals"
                "(approval_id,cycle_id,decision_id,tool,args_json,preview,provenance,"
                "rationale,evidence_json,ts,expires_at,required_approvals,"
                "signatures_json,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, '[]', 'pending')",
                (approval_id, cycle_id, decision_id, tool, json.dumps(args), preview,
                 provenance, rationale, evidence_json, time.time(), expires_at,
                 required_approvals),
            )
            self._conn.commit()

    def add_approval_signature(self, approval_id: str, approver: str,
                               notes: str = "") -> int:
        """Append a signature to an approval and return the new signature count."""
        with self._lock:
            row = self._conn.execute(
                "SELECT signatures_json FROM approvals WHERE approval_id=?",
                (approval_id,)).fetchone()
            sigs = json.loads(row["signatures_json"] if row else "[]") or []
            sigs.append({"approved_by": approver, "notes": notes, "ts": time.time()})
            self._conn.execute(
                "UPDATE approvals SET signatures_json=? WHERE approval_id=?",
                (json.dumps(sigs), approval_id))
            self._conn.commit()
            return len(sigs)

    def list_approvals(self, include_expired: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id,cycle_id,decision_id,tool,args_json,preview,"
                "provenance,rationale,evidence_json,ts,expires_at,resolved_at,"
                "approved_by,approval_notes,required_approvals,signatures_json,status "
                "FROM approvals WHERE status='pending' ORDER BY ts ASC",
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            d["signatures"] = json.loads(d.pop("signatures_json") or "[]")
            if not include_expired and d["expires_at"] and d["expires_at"] < now:
                continue
            out.append(d)
        return out

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT approval_id,cycle_id,decision_id,tool,args_json,preview,"
                "provenance,rationale,evidence_json,ts,expires_at,resolved_at,"
                "approved_by,approval_notes,required_approvals,signatures_json,status "
                "FROM approvals WHERE approval_id=?", (approval_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json") or "{}")
        d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
        d["signatures"] = json.loads(d.pop("signatures_json") or "[]")
        return d

    def list_all_approvals(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id,cycle_id,decision_id,tool,args_json,preview,"
                "provenance,rationale,evidence_json,ts,expires_at,resolved_at,"
                "approved_by,approval_notes,required_approvals,signatures_json,status "
                "FROM approvals ORDER BY ts DESC LIMIT ?", (limit,),
            ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            d["signatures"] = json.loads(d.pop("signatures_json") or "[]")
            out.append(d)
        return out

    def resolve_approval(self, approval_id: str, status: str,
                         approved_by: str = "", approval_notes: str = "") -> bool:
        """Atomically transition a still-pending approval. Returns True only if
        THIS call won the pending->status transition (guards cross-process
        double-execution of consequential actions)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status=?, resolved_at=?, approved_by=?, "
                "approval_notes=? "
                "WHERE approval_id=? AND status='pending'",
                (status, time.time(), approved_by, approval_notes, approval_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ----------------------------------------------------- compliance events
    def add_compliance_event(self, cycle_id: str, event_type: str,
                             payload: dict, ref_id: str = "",
                             ts: float | None = None) -> int:
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO compliance_events"
                "(cycle_id,event_type,ref_id,payload_json,ts) VALUES (?,?,?,?,?)",
                (cycle_id, event_type, ref_id, json.dumps(payload), ts),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_compliance_events(self, cycle_id: str | None = None,
                               limit: int = 500) -> list[dict]:
        with self._lock:
            if cycle_id is None:
                rows = self._conn.execute(
                    "SELECT cycle_id,event_type,ref_id,payload_json,ts "
                    "FROM compliance_events ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._conn.execute(
                    "SELECT cycle_id,event_type,ref_id,payload_json,ts "
                    "FROM compliance_events WHERE cycle_id=? ORDER BY id ASC",
                    (cycle_id,),
                ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    # ----------------------------------------------------------- vectors (RAG)
    def add_vector(self, ns: str, doc_id: str, chunk_idx: int, text: str,
                   provenance: str, kind: str, embedding: list[float],
                   ts: float | None = None) -> int:
        ts = time.time() if ts is None else ts
        blob = array.array("f", embedding).tobytes()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO vectors"
                "(ns,doc_id,chunk_idx,text,provenance,kind,embedding,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ns, doc_id, chunk_idx, text, provenance, kind, blob, ts),
            )
            self._conn.commit()
            self._vec_versions[ns] = self._vec_versions.get(ns, 0) + 1
            return int(cur.lastrowid or 0)

    def vector_version(self, ns: str) -> int:
        """Monotonic counter bumped on every add/delete in ``ns``. Lets callers
        cache a built index and rebuild only when the namespace changed."""
        return self._vec_versions.get(ns, 0)

    def fetch_vectors(self, ns: str) -> tuple[list[dict], list[bytes]]:
        """Return (metadata, raw embedding blobs) for a namespace.

        Blobs are returned undeserialized so an index can be built directly from
        bytes (numpy ``frombuffer``) without per-row Python float allocation."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id,chunk_idx,text,provenance,kind,embedding,ts "
                "FROM vectors WHERE ns=? ORDER BY id ASC", (ns,),
            ).fetchall()
        metas: list[dict] = []
        blobs: list[bytes] = []
        for r in rows:
            d = dict(r)
            blobs.append(bytes(d.pop("embedding")))
            metas.append(d)
        return metas, blobs

    def iter_vectors(self, ns: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id,chunk_idx,text,provenance,kind,embedding,ts "
                "FROM vectors WHERE ns=? ORDER BY id ASC", (ns,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            vec = array.array("f")
            vec.frombytes(d.pop("embedding"))
            d["embedding"] = list(vec)
            out.append(d)
        return out

    def list_namespaces(self) -> list[str]:
        """Distinct vector namespaces that currently hold indexed chunks.

        Lets retrieval span every registered RAG repository instead of only the
        default 'kb' namespace, so grounded answers draw on all sources.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ns FROM vectors ORDER BY ns").fetchall()
        return [r["ns"] for r in rows]

    def count_vectors(self, ns: str | None = None) -> int:
        with self._lock:
            if ns is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM vectors").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM vectors WHERE ns=?", (ns,)).fetchone()
        return int(row["n"])

    def doc_ids(self, ns: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT doc_id FROM vectors WHERE ns=? ORDER BY doc_id",
                (ns,)).fetchall()
        return [r["doc_id"] for r in rows]

    def doc_latest_ts(self, ns: str, doc_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS m FROM vectors WHERE ns=? AND doc_id=?",
                (ns, doc_id)).fetchone()
        return float(row["m"]) if row and row["m"] is not None else 0.0

    def delete_doc(self, ns: str, doc_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM vectors WHERE ns=? AND doc_id=?", (ns, doc_id))
            self._conn.commit()
            if cur.rowcount:
                self._vec_versions[ns] = self._vec_versions.get(ns, 0) + 1
            return cur.rowcount

    # --------------------------------------------------------------- tasks
    def add_task(self, task_id: str, goal: str, status: str = "pending",
                 max_attempts: int = 3, next_retry_ts: float | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id,goal,status,attempts,max_attempts,"
                "created_ts,updated_ts,next_retry_ts) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, goal, status, 0, max_attempts, now, now, next_retry_ts),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id,goal,status,attempts,max_attempts,created_ts,"
                "updated_ts,next_retry_ts,cycle_id,result_json,error "
                "FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json") or "{}")
        return d

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT task_id,goal,status,attempts,max_attempts,created_ts,"
                    "updated_ts,next_retry_ts,cycle_id,result_json,error "
                    "FROM tasks WHERE status=? ORDER BY created_ts DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT task_id,goal,status,attempts,max_attempts,created_ts,"
                    "updated_ts,next_retry_ts,cycle_id,result_json,error "
                    "FROM tasks ORDER BY created_ts DESC LIMIT ?", (limit,),
                ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["result"] = json.loads(d.pop("result_json") or "{}")
            out.append(d)
        return out

    def update_task(self, task_id: str, **fields) -> bool:
        allowed = {
            "status", "attempts", "next_retry_ts", "cycle_id", "result_json",
            "error", "updated_ts", "output", "plan"
        }
        fields.setdefault("updated_ts", time.time())
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [task_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id=?", vals)
            self._conn.commit()
            return cur.rowcount == 1

    # ------------------------------------------------------------ KB sources
    @staticmethod
    def stable_source_id(uri: str, ns: str = "kb") -> str:
        return "src-" + hashlib.sha1(f"{ns}:{uri}".encode()).hexdigest()[:12]

    def upsert_kb_source(self, uri: str, source_type: str, ns: str = "kb",
                         title: str = "", refresh_interval_seconds: float | None = None,
                         enabled: bool = True, source_id: str | None = None) -> str:
        now = time.time()
        sid = source_id or self.stable_source_id(uri, ns)
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM kb_sources WHERE source_id=?",
                (sid,),
            ).fetchone()
            created = existing["created_ts"] if existing else now
            self._conn.execute(
                "INSERT OR REPLACE INTO kb_sources"
                "(source_id,uri,source_type,ns,title,refresh_interval_seconds,"
                "enabled,status,created_ts,updated_ts,last_hash,last_ingested_ts,error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, uri, source_type, ns, title, refresh_interval_seconds,
                 1 if enabled else 0, "pending", created, now,
                 (existing["last_hash"] if existing and "last_hash" in existing.keys() else ""),
                 (existing["last_ingested_ts"] if existing and "last_ingested_ts" in existing.keys() else None),
                 (existing["error"] if existing and "error" in existing.keys() else "")),
            )
            self._conn.commit()
        return sid

    def get_kb_source(self, source_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kb_sources WHERE source_id=?", (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_kb_sources(self, enabled: bool | None = None) -> list[dict]:
        with self._lock:
            if enabled is None:
                rows = self._conn.execute(
                    "SELECT * FROM kb_sources ORDER BY updated_ts DESC").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM kb_sources WHERE enabled=? ORDER BY updated_ts DESC",
                    (1 if enabled else 0,),
                ).fetchall()
        return [dict(r) for r in rows]

    def due_kb_sources(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        due = []
        for src in self.list_kb_sources(enabled=True):
            interval = src.get("refresh_interval_seconds")
            last = src.get("last_ingested_ts")
            if last is None or (interval is not None and last + interval <= now):
                due.append(src)
        return due

    def update_kb_source_refresh(self, source_id: str, status: str,
                                 last_hash: str | None = None,
                                 error: str = "",
                                 ingested: bool = False) -> None:
        now = time.time()
        fields = {"status": status, "error": error, "updated_ts": now}
        if last_hash is not None:
            fields["last_hash"] = last_hash
        if ingested:
            fields["last_ingested_ts"] = now
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [source_id]
        with self._lock:
            self._conn.execute(f"UPDATE kb_sources SET {cols} WHERE source_id=?", vals)
            self._conn.commit()

    def delete_kb_source(self, source_id: str) -> bool:
        """Delete a KB/wiki source by id. Returns True if a row was removed.

        Also drops the source's ingested RAG vectors. KBSourceManager.refresh
        uses the source_id itself as the RAG doc id, so delete_doc(ns, source_id)
        removes exactly the chunks this source contributed — preventing a removed
        source from continuing to pollute retrieval.
        """
        row = self.get_kb_source(source_id)
        if row is not None:
            # delete_doc takes its own lock/commit and bumps the vector version;
            # call it outside the lock below to avoid re-entrant locking.
            self.delete_doc(row.get("ns", "kb"), source_id)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM kb_sources WHERE source_id=?", (source_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    # ------------------------------------------------------------- cron jobs
    def add_cron_job(self, job_id: str, goal: str, schedule: str,
                     name: str = "", mode: str = "do", deliver: str = "local",
                     next_run_ts: float | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO cron_jobs(job_id,name,goal,schedule,mode,deliver,"
                "enabled,next_run_ts,created_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,1,?,?,?)",
                (job_id, name, goal, schedule, mode, deliver, next_run_ts,
                 now, now))
            self._conn.commit()

    def list_cron_jobs(self, enabled: bool | None = None) -> list[dict]:
        q = "SELECT * FROM cron_jobs"
        params: tuple = ()
        if enabled is not None:
            q += " WHERE enabled=?"
            params = (1 if enabled else 0,)
        q += " ORDER BY created_ts ASC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_cron_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cron_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def due_cron_jobs(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cron_jobs WHERE enabled=1 AND next_run_ts IS NOT NULL "
                "AND next_run_ts <= ? ORDER BY next_run_ts ASC", (now,)).fetchall()
        return [dict(r) for r in rows]

    def set_cron_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cron_jobs SET enabled=?, updated_ts=? WHERE job_id=?",
                (1 if enabled else 0, time.time(), job_id))
            self._conn.commit()
            return bool(cur.rowcount)

    def update_cron_after_run(self, job_id: str, next_run_ts: float | None,
                              status: str, output: str = "") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE cron_jobs SET last_run_ts=?, next_run_ts=?, "
                "last_status=?, last_output=?, runs=runs+1, updated_ts=? "
                "WHERE job_id=?",
                (now, next_run_ts, status, output[:2000], now, job_id))
            self._conn.commit()

    def set_cron_next_run(self, job_id: str, next_run_ts: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cron_jobs SET next_run_ts=?, updated_ts=? WHERE job_id=?",
                (next_run_ts, time.time(), job_id))
            self._conn.commit()

    def delete_cron_job(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cron_jobs WHERE job_id=?", (job_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------- skill metrics
    def record_skill_outcome(self, skill_name: str, goal: str, outcome: str,
                             score: float, cycle_id: str = "",
                             notes: str = "") -> None:
        now = time.time()
        success = 1 if outcome == "success" else 0
        failure = 1 if outcome == "failure" else 0
        with self._lock:
            self._conn.execute(
                "INSERT INTO skill_outcomes"
                "(skill_name,goal,outcome,score,cycle_id,notes,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (skill_name, goal, outcome, score, cycle_id, notes, now),
            )
            meta = self._conn.execute(
                "SELECT usage_count,success_count,failure_count FROM skill_metadata "
                "WHERE skill_name=?", (skill_name,),
            ).fetchone()
            if meta:
                usage = meta["usage_count"] + 1
                successes = meta["success_count"] + success
                failures = meta["failure_count"] + failure
            else:
                usage, successes, failures = 1, success, failure
            quality = successes / usage if usage else 0.0
            self._conn.execute(
                "INSERT OR REPLACE INTO skill_metadata"
                "(skill_name,usage_count,success_count,failure_count,quality_score,"
                "last_used_ts,quarantined,updated_ts) VALUES (?,?,?,?,?,?,"
                "COALESCE((SELECT quarantined FROM skill_metadata WHERE skill_name=?), 0),?)",
                (skill_name, usage, successes, failures, quality, now, skill_name, now),
            )
            self._conn.commit()

    def skill_metadata(self, skill_name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM skill_metadata WHERE skill_name=?", (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_skill_metadata(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM skill_metadata ORDER BY quality_score ASC, updated_ts DESC",
            ).fetchall()
        return [dict(r) for r in rows]

    def set_skill_quarantine(self, skill_name: str, quarantined: bool) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO skill_metadata(skill_name,quarantined,updated_ts) "
                "VALUES (?,?,?) ON CONFLICT(skill_name) DO UPDATE SET "
                "quarantined=excluded.quarantined, updated_ts=excluded.updated_ts",
                (skill_name, 1 if quarantined else 0, now),
            )
            self._conn.commit()

    # --------------------------------------------------------------- agents
    def upsert_agent_instance(self, agent_id: str, role: str,
                              tools: list[str] | None = None,
                              status: str = "idle", load: int = 0,
                              metrics: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_ts FROM agent_instances WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            created = existing["created_ts"] if existing else now
            self._conn.execute(
                "INSERT OR REPLACE INTO agent_instances"
                "(agent_id,role,tools_json,status,load,last_heartbeat_ts,"
                "metrics_json,created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (agent_id, role, json.dumps(tools or []), status, load, now,
                 json.dumps(metrics or {}), created, now),
            )
            self._conn.commit()

    def list_agent_instances(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_instances ORDER BY role, agent_id").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["tools"] = json.loads(d.pop("tools_json") or "[]")
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            out.append(d)
        return out

    def add_subagent_run(self, run_id: str, agent_id: str, role: str,
                         goal: str, status: str = "running") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO subagent_runs"
                "(run_id,agent_id,role,goal,status,created_ts,updated_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, agent_id, role, goal, status, now, now),
            )
            self._conn.commit()

    def update_subagent_run(self, run_id: str, **fields) -> bool:
        allowed = {"status", "cycle_id", "result_json", "error", "updated_ts"}
        fields.setdefault("updated_ts", time.time())
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [run_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE subagent_runs SET {cols} WHERE run_id=?", vals)
            self._conn.commit()
            return cur.rowcount == 1

    def list_subagent_runs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subagent_runs ORDER BY created_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["result"] = json.loads(d.pop("result_json") or "{}")
            out.append(d)
        return out

    # ----------------------------------------------------- learned router model
    def save_router_model(self, model_json: str, n_samples: int = 0,
                          name: str = "predictive_router") -> None:
        """Persist a trained goal->role router model (JSON) under ``name``."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO router_models(name,model_json,n_samples,trained_ts) "
                "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "model_json=excluded.model_json, n_samples=excluded.n_samples, "
                "trained_ts=excluded.trained_ts",
                (name, model_json, n_samples, time.time()),
            )
            self._conn.commit()

    def load_router_model(self, name: str = "predictive_router") -> dict | None:
        """Return the persisted router model record, or ``None`` if untrained."""
        with self._lock:
            row = self._conn.execute(
                "SELECT name,model_json,n_samples,trained_ts FROM router_models "
                "WHERE name=?", (name,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------- eval history/gate
    def save_eval_run(self, report_json: str, passes: int, total: int) -> int:
        """Append one eval scorecard to the run history; returns its run id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO eval_runs(ts,passes,total,report_json) VALUES (?,?,?,?)",
                (time.time(), passes, total, report_json),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_eval_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,ts,passes,total FROM eval_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_eval_baseline(self, report_json: str, name: str = "default") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO eval_baseline(name,report_json,ts) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET report_json=excluded.report_json, "
                "ts=excluded.ts", (name, report_json, time.time()),
            )
            self._conn.commit()

    def load_eval_baseline(self, name: str = "default") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM eval_baseline WHERE name=?", (name,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["report_json"])
        except ValueError:
            return None

    # ------------------------------------------------------------- run traces
    def start_run(self, run_id: str, goal: str = "", kind: str = "plan") -> None:
        """Open a durable, replayable run trace (idempotent on ``run_id``)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(run_id,goal,kind,status,started_ts,event_count) "
                "VALUES (?,?,?,?,?,0) ON CONFLICT(run_id) DO NOTHING",
                (run_id, goal, kind, "running", time.time()),
            )
            self._conn.commit()

    def add_run_event(self, run_id: str, kind: str, data: dict | None = None,
                      node_id: str = "", label: str = "") -> int:
        """Append one event to a run trace; returns its per-run sequence number.

        The trace is durable and replayable — unlike an ephemeral in-memory
        activity feed, ``list_run_events`` can reconstruct the run after the fact.
        """
        payload = json.dumps(data or {}, default=str)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            seq = int(row["n"])
            self._conn.execute(
                "INSERT INTO run_events(run_id,seq,ts,kind,node_id,label,data_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, seq, time.time(), kind, node_id, label, payload),
            )
            self._conn.execute(
                "UPDATE runs SET event_count=event_count+1 WHERE run_id=?", (run_id,),
            )
            self._conn.commit()
        return seq

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, ended_ts=? WHERE run_id=?",
                (status, time.time(), run_id),
            )
            self._conn.commit()

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id,goal,kind,status,started_ts,ended_ts,event_count "
                "FROM runs ORDER BY started_ts DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id,goal,kind,status,started_ts,ended_ts,event_count "
                "FROM runs WHERE run_id=?", (run_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- run routing
    def record_run_routing(self, run_id: str, model: str, prompt_tokens: int,
                           completion_tokens: int, cost_usd: float, calls: int,
                           local: bool, fallbacks: int = 0,
                           escalations: int = 0, escalation_reason: str = "") -> None:
        """Persist which model handled a run + its tokens/cost — routing legibility."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_routing(run_id,model,prompt_tokens,"
                "completion_tokens,cost_usd,calls,local,fallbacks,escalations,"
                "escalation_reason,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, model, int(prompt_tokens), int(completion_tokens),
                 float(cost_usd), int(calls), 1 if local else 0,
                 int(fallbacks), int(escalations), str(escalation_reason or ""),
                 time.time()))
            self._conn.commit()

    def list_run_routing(self, limit: int = 20) -> list[dict]:
        """Recent per-run routing decisions joined with the run's goal/status."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rr.run_id, rr.model, rr.prompt_tokens, rr.completion_tokens, "
                "rr.cost_usd, rr.calls, rr.local, rr.fallbacks, rr.escalations, "
                "rr.escalation_reason, rr.ts, "
                "r.goal AS goal, r.status AS status "
                "FROM run_routing rr LEFT JOIN runs r ON r.run_id = rr.run_id "
                "ORDER BY rr.ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def routing_cost_stats(self, trend_limit: int = 24) -> dict:
        """Spend aggregates from ``run_routing``: grand totals, per-model cost,
        and a recent per-run cost trend — powers the Metrics spend charts."""
        with self._lock:
            per_model = self._conn.execute(
                "SELECT model, COUNT(*) runs, "
                "SUM(prompt_tokens+completion_tokens) tokens, "
                "SUM(cost_usd) cost, SUM(local) local_runs "
                "FROM run_routing GROUP BY model ORDER BY cost DESC, runs DESC"
            ).fetchall()
            totals = self._conn.execute(
                "SELECT COUNT(*) runs, "
                "COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, "
                "COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(local),0) local_runs "
                "FROM run_routing"
            ).fetchone()
            trend_rows = self._conn.execute(
                "SELECT rr.run_id, rr.model, rr.cost_usd, rr.local, rr.ts, "
                "r.goal AS goal FROM run_routing rr "
                "LEFT JOIN runs r ON r.run_id = rr.run_id "
                "ORDER BY rr.ts DESC LIMIT ?", (trend_limit,)
            ).fetchall()
        by_model = [
            {"model": (r["model"] or "\u2014"), "runs": int(r["runs"]),
             "tokens": int(r["tokens"] or 0), "cost_usd": float(r["cost"] or 0.0),
             "local_runs": int(r["local_runs"] or 0)}
            for r in per_model
        ]
        trend = [
            {"run_id": r["run_id"], "model": (r["model"] or "\u2014"),
             "cost_usd": float(r["cost_usd"] or 0.0), "local": bool(r["local"]),
             "ts": float(r["ts"]), "goal": r["goal"]}
            for r in reversed(trend_rows)
        ]
        return {
            "total_cost_usd": float(totals["cost"] or 0.0),
            "total_tokens": int(totals["tokens"] or 0),
            "total_runs": int(totals["runs"] or 0),
            "local_runs": int(totals["local_runs"] or 0),
            "by_model": by_model,
            "trend": trend,
        }

    def list_run_events(self, run_id: str, limit: int = 2000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,ts,kind,node_id,label,data_json FROM run_events "
                "WHERE run_id=? ORDER BY seq ASC LIMIT ?", (run_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.pop("data_json") or "{}")
            except ValueError:
                d["data"] = {}
            out.append(d)
        return out

    # ------------------------------------------------------------- audit list
    def list_audit(self, limit: int = 100) -> list[dict]:
        """Recent governed decisions, newest first — powers the audit viewer."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,ts,actor,tool,risk,verdict,detail,policy_rule,"
                "approval_id,cycle_id FROM audit_entries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def audit_stats(self) -> dict:
        """Decision mix by verdict and policy rule — powers the metrics panel."""
        with self._lock:
            vr = self._conn.execute(
                "SELECT verdict, COUNT(*) n FROM audit_entries GROUP BY verdict",
            ).fetchall()
            rr = self._conn.execute(
                "SELECT policy_rule, COUNT(*) n FROM audit_entries "
                "WHERE policy_rule != '' GROUP BY policy_rule",
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) n FROM audit_entries").fetchone()["n"]
        return {
            "by_verdict": {r["verdict"]: r["n"] for r in vr},
            "by_rule": {r["policy_rule"]: r["n"] for r in rr},
            "total": int(total),
        }

    def run_stats(self) -> dict:
        """Run-trace counts by final status — powers the metrics panel."""
        with self._lock:
            rs = self._conn.execute(
                "SELECT status, COUNT(*) n FROM runs GROUP BY status").fetchall()
            total = self._conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        return {"by_status": {r["status"]: r["n"] for r in rs}, "total": int(total)}

    # ----------------------------------------------------------------- budget
    def get_budget(self, name: str = "default") -> dict:
        """Fetch (creating on first use) the spend budget for ``name``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT name,limit_usd,spent_usd,runs,period_start FROM budget "
                "WHERE name=?", (name,)).fetchone()
            if row is None:
                now = time.time()
                self._conn.execute(
                    "INSERT INTO budget(name,limit_usd,spent_usd,runs,period_start) "
                    "VALUES (?,?,?,?,?)", (name, 0.0, 0.0, 0, now))
                self._conn.commit()
                return {"name": name, "limit_usd": 0.0, "spent_usd": 0.0,
                        "runs": 0, "period_start": now}
        return dict(row)

    def set_budget_limit(self, limit_usd: float, name: str = "default") -> dict:
        self.get_budget(name)
        with self._lock:
            self._conn.execute(
                "UPDATE budget SET limit_usd=? WHERE name=?",
                (max(0.0, float(limit_usd)), name))
            self._conn.commit()
        return self.get_budget(name)

    def add_spend(self, amount: float, name: str = "default", *,
                  count_run: bool = True) -> dict:
        self.get_budget(name)
        with self._lock:
            if count_run:
                self._conn.execute(
                    "UPDATE budget SET spent_usd=spent_usd+?, runs=runs+1 "
                    "WHERE name=?", (max(0.0, float(amount)), name))
            else:
                self._conn.execute(
                    "UPDATE budget SET spent_usd=spent_usd+? WHERE name=?",
                    (max(0.0, float(amount)), name))
            self._conn.commit()
        return self.get_budget(name)

    def reset_budget(self, name: str = "default") -> dict:
        self.get_budget(name)
        with self._lock:
            self._conn.execute(
                "UPDATE budget SET spent_usd=0, runs=0, period_start=? WHERE name=?",
                (time.time(), name))
            self._conn.commit()
        return self.get_budget(name)

    # ------------------------------------------------------------- killswitch
    def get_killswitch(self, name: str = "default") -> bool:
        """Whether the governance kill-switch is engaged (persisted across restarts)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT engaged FROM killswitch WHERE name=?", (name,)).fetchone()
        return bool(row["engaged"]) if row is not None else False

    def set_killswitch(self, engaged: bool, name: str = "default") -> bool:
        """Persist the kill-switch state so an engaged brake survives a restart."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO killswitch(name,engaged,updated_ts) "
                "VALUES (?,?,?)", (name, 1 if engaged else 0, time.time()))
            self._conn.commit()
        return bool(engaged)

    # ------------------------------------------------------------- compliance
    def get_compliance_mode(self, name: str = "default") -> str:
        """The persisted governance compliance mode (defaults to 'enforced')."""
        with self._lock:
            row = self._conn.execute(
                "SELECT mode FROM compliance WHERE name=?", (name,)).fetchone()
        return str(row["mode"]) if row is not None else "enforced"

    def get_compliance_expiry(self, name: str = "default") -> float | None:
        """When a timed relaxation auto-reverts to enforced (epoch seconds), or
        None for an open-ended mode."""
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_ts FROM compliance WHERE name=?", (name,)).fetchone()
        if row is None or row["expires_ts"] is None:
            return None
        return float(row["expires_ts"])

    def set_compliance_mode(self, mode: str, expires_ts: float | None = None,
                            name: str = "default") -> str:
        """Persist the compliance mode (and optional auto-revert time) so the
        operator's choice survives restarts."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO compliance(name,mode,expires_ts,updated_ts) "
                "VALUES (?,?,?,?)", (name, mode, expires_ts, time.time()))
            self._conn.commit()
        return mode

    # ------------------------------------------------------------- work board
    def add_card(self, card_id: str, title: str, goal: str,
                 lane: str = "backlog") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO board_cards(card_id,title,goal,lane,run_id,status,"
                "created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (card_id, title, goal, lane, "", "", now, now),
            )
            self._conn.commit()

    def list_cards(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_id,title,goal,lane,run_id,status,created_ts,updated_ts "
                "FROM board_cards ORDER BY created_ts ASC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_card(self, card_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT card_id,title,goal,lane,run_id,status,created_ts,updated_ts "
                "FROM board_cards WHERE card_id=?", (card_id,),
            ).fetchone()
        return dict(row) if row else None

    def move_card(self, card_id: str, lane: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE board_cards SET lane=?, updated_ts=? WHERE card_id=?",
                (lane, time.time(), card_id),
            )
            self._conn.commit()

    def set_card_run(self, card_id: str, run_id: str, status: str,
                     lane: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE board_cards SET run_id=?, status=?, lane=?, updated_ts=? "
                "WHERE card_id=?", (run_id, status, lane, time.time(), card_id),
            )
            self._conn.commit()

    def delete_card(self, card_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM board_cards WHERE card_id=?", (card_id,))
            self._conn.commit()
