"""Managed knowledge-base / wiki source registry and refresh loop.

``praxis ingest`` is useful for one-off documents; regulated deployments need a
managed source list with change detection, freshness, and periodic revalidation.
This module registers file/URL/wiki-like sources, hashes their current content,
and re-ingests only when the source changed or has never been ingested.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .ingest import extract_text
from .rag import Rag
from .wiki_safe import UnsafeSourceError, fetch_url, validate_uri


@dataclass
class KBSource:
    source_id: str
    uri: str
    source_type: str
    ns: str = "kb"
    title: str = ""
    status: str = "pending"
    last_hash: str = ""
    last_ingested_ts: float | None = None
    refresh_interval_seconds: float | None = None
    enabled: bool = True
    error: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "KBSource":
        return cls(
            source_id=row["source_id"], uri=row["uri"],
            source_type=row["source_type"], ns=row.get("ns", "kb"),
            title=row.get("title", ""), status=row.get("status", "pending"),
            last_hash=row.get("last_hash", ""),
            last_ingested_ts=row.get("last_ingested_ts"),
            refresh_interval_seconds=row.get("refresh_interval_seconds"),
            enabled=bool(row.get("enabled", 1)), error=row.get("error", ""),
        )


class KBSourceManager:
    def __init__(self, store) -> None:
        self.store = store

    def add(self, uri: str, source_type: str | None = None, ns: str = "kb",
            title: str = "", refresh_interval_seconds: float | None = None,
            enabled: bool = True) -> KBSource:
        stype = source_type or self._infer_type(uri)
        # Validate up front so an operator (or an LLM-generated suggestion) is
        # rejected at registration time rather than silently failing later.
        if stype == "url":
            validate_uri(uri)
        elif stype == "file":
            if not Path(uri).exists():
                raise UnsafeSourceError(f"file source not found: {uri}")
        else:
            raise UnsafeSourceError(
                f"refusing unknown source_type {stype!r}: use 'url' or 'file'.")
        sid = self.store.upsert_kb_source(
            uri=uri, source_type=stype, ns=ns, title=title,
            refresh_interval_seconds=refresh_interval_seconds, enabled=enabled)
        return self.get(sid)

    def get(self, source_id: str) -> KBSource | None:
        row = self.store.get_kb_source(source_id)
        return KBSource.from_row(row) if row else None

    def list(self, enabled: bool | None = None) -> list[KBSource]:
        return [KBSource.from_row(r) for r in self.store.list_kb_sources(enabled)]

    def due(self) -> list[KBSource]:
        return [KBSource.from_row(r) for r in self.store.due_kb_sources()]

    def refresh_due(self, rag: Rag | None = None) -> list[KBSource]:
        out = []
        for src in self.due():
            out.append(self.refresh(src.source_id, rag=rag))
        return out

    def refresh(self, source_id: str, rag: Rag | None = None) -> KBSource:
        rag = rag or Rag(self.store)
        src = self.get(source_id)
        if src is None:
            raise KeyError(source_id)
        # Use the stable source_id as the RAG doc id so two sources with the
        # same human title can't clobber each other's vectors.
        doc_id = source_id
        try:
            text, kind, source_name = self._read_source(src)
            digest = hashlib.sha256(text.encode()).hexdigest()
            if digest == src.last_hash:
                # Advance last_ingested_ts so an unchanged source stops being
                # "due" until the interval elapses again — otherwise every
                # refresh_due cycle re-fetches it forever.
                self.store.update_kb_source_refresh(
                    source_id, "unchanged", last_hash=digest, error="",
                    ingested=True)
            else:
                rag.ingest_text(
                    text, source=doc_id, kind=kind,
                    provenance=f"{src.source_type}:{src.uri}", ns=src.ns)
                self.store.update_kb_source_refresh(
                    source_id, "refreshed", last_hash=digest,
                    error="", ingested=True)
            self.store.add_compliance_event("", "kb_source_refreshed", {
                "source_id": source_id, "uri": src.uri, "status": self.get(source_id).status,
            }, ref_id=source_id)
        except Exception as exc:
            self.store.update_kb_source_refresh(source_id, "error", error=str(exc))
            self.store.add_compliance_event("", "kb_source_error", {
                "source_id": source_id, "uri": src.uri, "error": str(exc),
            }, ref_id=source_id)
        return self.get(source_id)

    @staticmethod
    def _infer_type(uri: str) -> str:
        if uri.startswith("http://") or uri.startswith("https://"):
            return "url"
        return "file"

    @staticmethod
    def _read_source(src: KBSource) -> tuple[str, str, str]:
        if src.source_type == "url":
            raw = fetch_url(src.uri)
            title = src.title or src.uri
            return raw, "wiki", title
        path = Path(src.uri)
        doc = extract_text(path)
        return doc.text, doc.kind, doc.source

    @staticmethod
    def seconds_from_hours(hours: float | None) -> float | None:
        return None if hours is None else max(0.0, hours * 3600.0)
