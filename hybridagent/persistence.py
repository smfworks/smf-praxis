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
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_tier ON memory_items(tier);

CREATE TABLE IF NOT EXISTS audit_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    actor   TEXT NOT NULL,
    tool    TEXT NOT NULL,
    risk    TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail  TEXT NOT NULL,
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL DEFAULT '{}',
    preview     TEXT NOT NULL DEFAULT '',
    provenance  TEXT NOT NULL DEFAULT 'plan',
    ts          REAL NOT NULL,
    expires_at  REAL,
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
"""


class Store:
    """Thread-safe SQLite wrapper. One connection, guarded by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Store":
        target = Path(path) if path else cfg.home_dir() / "praxis.db"
        return cls(target)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- memory
    def add_memory(self, tier: str, text: str, provenance: str,
                   kind: str, ts: float | None = None) -> int:
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_items(tier,text,provenance,kind,ts) "
                "VALUES (?,?,?,?,?)",
                (tier, text, provenance, kind, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def load_memory(self, tier: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT text,provenance,kind,ts FROM memory_items "
                "WHERE tier=? ORDER BY id ASC", (tier,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- audit
    def add_audit(self, actor: str, tool: str, risk: str, verdict: str,
                  detail: str, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_entries(actor,tool,risk,verdict,detail,ts) "
                "VALUES (?,?,?,?,?,?)",
                (actor, tool, risk, verdict, detail, ts),
            )
            self._conn.commit()

    def load_audit(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT actor,tool,risk,verdict,detail,ts FROM audit_entries "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ----------------------------------------------------------- approvals
    def upsert_approval(self, approval_id: str, tool: str, args: dict,
                        preview: str, provenance: str,
                        expires_at: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO approvals"
                "(approval_id,tool,args_json,preview,provenance,ts,expires_at,status) "
                "VALUES (?,?,?,?,?,?,?, 'pending')",
                (approval_id, tool, json.dumps(args), preview, provenance,
                 time.time(), expires_at),
            )
            self._conn.commit()

    def list_approvals(self, include_expired: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id,tool,args_json,preview,provenance,ts,expires_at "
                "FROM approvals WHERE status='pending' ORDER BY ts ASC",
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            if not include_expired and d["expires_at"] and d["expires_at"] < now:
                continue
            out.append(d)
        return out

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT approval_id,tool,args_json,preview,provenance,ts,expires_at,status "
                "FROM approvals WHERE approval_id=?", (approval_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json") or "{}")
        return d

    def resolve_approval(self, approval_id: str, status: str) -> bool:
        """Atomically transition a still-pending approval. Returns True only if
        THIS call won the pending->status transition (guards cross-process
        double-execution of consequential actions)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status=? "
                "WHERE approval_id=? AND status='pending'",
                (status, approval_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

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
            return int(cur.lastrowid)

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
            return cur.rowcount
