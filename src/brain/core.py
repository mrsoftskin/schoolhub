"""Core facade: one object bundling config, storage, embeddings, retrieval,
ask, and calendar. The CLI and the web app both talk to this and nothing
deeper - no retrieval logic lives in route handlers or command bodies.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from sqlite3 import Connection

from . import calendar as cal
from .ask import GLOBAL_COLLECTION, AskPrepared, prepare_ask, stream_answer
from .config import Config, find_config, load_config
from .db import connect
from .embeddings import Embedder, OnnxBgeEmbedder
from .env import load_env_file
from .errors import IndexBusy
from .indexer import IndexReport, index_collections
from .retrieval import Retriever

DB_FILE = "brain.db"

# Process-wide index serialization. Indexing rewrites the shared embedding
# store (embeddings.npy plus its id/hash manifest), so two runs interleaving
# can commit a vector against text it was not built from - the same failure
# class as the rowid-reuse drift the content hashes exist to catch.
#
# This lives HERE, not in the web layer, because the web layer's guard only
# covered the routes it owned: SyncPoller._pull_new_content() called
# Core.index() directly and bypassed it entirely, and that path is reachable
# from the daemon loop and from every /api/sync/* route.
#
# In-process only. A `brain index` run in a separate terminal is still not
# serialized against a running server; that needs a file lock.
_INDEX_LOCK = threading.Lock()


def index_busy() -> bool:
    """Is an index run in flight in this process?"""
    return _INDEX_LOCK.locked()


class Core:
    def __init__(self, config: Config, embedder: Embedder | None = None):
        self.config = config
        # ONNX by default: identical vectors to the PyTorch path (verified
        # cosine 1.0), a fraction of the install size, and it works on Intel
        # Macs, which torch no longer supports.
        self.embedder: Embedder = embedder or OnnxBgeEmbedder(config.settings.embedding_model)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Core":
        path = find_config(config_path)
        # Before anything reads ANTHROPIC_API_KEY.
        load_env_file(path)
        return cls(load_config(path))

    @property
    def db_path(self) -> Path:
        return self.config.settings.data_dir / DB_FILE

    def open_db(self) -> Connection:
        return connect(self.db_path)

    # ---- indexing ------------------------------------------------------

    def index(self, *, only: list[str] | None = None, force: bool = False,
              progress=None, wait: bool = True) -> IndexReport:
        """Index the named collections.

        Serialized process-wide (see _INDEX_LOCK). wait=False raises IndexBusy
        rather than queueing, for callers like the background poller where
        deferring to the next cycle is better than blocking.
        """
        if not _INDEX_LOCK.acquire(blocking=wait):
            raise IndexBusy("An index run is already in progress.")
        try:
            conn = self.open_db()
            try:
                return index_collections(
                    self.config, conn, self.embedder,
                    only=only, force=force, progress=progress,
                )
            finally:
                conn.close()
        finally:
            _INDEX_LOCK.release()

    def retriever(self, conn: Connection) -> Retriever:
        return Retriever(self.config, conn, self.embedder)

    # ---- ask -----------------------------------------------------------

    def prepare_ask(self, conn: Connection, question: str, collection: str,
                    *, k: int | None = None, history: list[dict] | None = None,
                    model: str | None = None,
                    has_images: bool = False) -> AskPrepared:
        return prepare_ask(self.config, conn, self.retriever(conn), question,
                           collection, k=k, history=history, model=model,
                           has_images=has_images)

    def stream_answer(self, prepared: AskPrepared, *, model: str | None = None,
                      history: list[dict] | None = None,
                      images: list[dict] | None = None):
        return stream_answer(self.config, prepared, model=model,
                             history=history, images=images)

    def backend_status(self) -> tuple[bool, str]:
        """Can the configured answer backend actually run? (ok, why-not)."""
        if self.config.settings.backend == "subscription":
            from . import agentsdk

            return agentsdk.available()
        from . import providers

        return providers.status(self.config.settings.backend)

    # ---- calendar ------------------------------------------------------

    def calendar_import(self) -> cal.CalendarImportReport:
        conn = self.open_db()
        try:
            report = cal.import_calendar(self.config, conn)
        finally:
            conn.close()
        self._save_calendar_status(report)
        return report

    def _calendar_status_path(self) -> Path:
        return self.config.settings.data_dir / "calendar_status.json"

    def _save_calendar_status(self, report: cal.CalendarImportReport) -> None:
        from datetime import datetime, timezone

        payload = {
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_imported": report.total_imported,
            "total_stored": report.total_stored,
            "upsert_only": report.upsert_only(),
            "sources": [
                {
                    "source": s.source, "detail": s.detail, "imported": s.imported,
                    "stored": s.stored, "errors": s.errors, "status": s.status,
                }
                for s in report.sources
            ],
        }
        self._calendar_status_path().parent.mkdir(parents=True, exist_ok=True)
        self._calendar_status_path().write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def calendar_status(self) -> dict | None:
        p = self._calendar_status_path()
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    # ---- library / status ---------------------------------------------

    def collection_stats(self, conn: Connection) -> list[dict]:
        status = {
            r["collection"]: dict(r)
            for r in conn.execute("SELECT * FROM index_status")
        }
        out = []
        for col in self.config.collections:
            st = status.get(col.name, {})
            out.append({
                "name": col.name,
                "color": col.color,
                "assist_level": col.assist_level,
                "roots": [str(r) for r in col.roots],
                "missing_roots": [str(r) for r in col.roots if not r.exists()],
                "doc_count": st.get("doc_count", 0),
                "chunk_count": st.get("chunk_count", 0),
                "last_indexed": st.get("last_indexed"),
                "failures": json.loads(st.get("failures_json") or "[]"),
            })
        return out


__all__ = ["Core", "GLOBAL_COLLECTION"]
