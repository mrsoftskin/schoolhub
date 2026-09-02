"""Brute-force cosine retrieval over the embedding matrix.

Single-collection search returns the top-k chunks above the similarity floor.
Global search runs top-k PER COLLECTION separately - there is no merged
candidate pool, so one big collection can never crowd the others out.
"""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

import numpy as np

from .config import Config
from .embeddings import Embedder, EmbeddingStore
from .errors import BrainError, EmptyIndexError, StoreOutOfSync


@dataclass
class Hit:
    chunk_id: int
    collection: str
    source_path: str
    locator: str
    text: str
    score: float


class Retriever:
    """Loads the matrix once and serves searches against it."""

    def __init__(self, config: Config, conn: Connection, embedder: Embedder):
        self.config = config
        self.conn = conn
        self.embedder = embedder
        store = EmbeddingStore(config.settings.data_dir, config.settings.embedding_model)
        self.ids, self.matrix, hashes = store.load()
        self.row_of = {cid: i for i, cid in enumerate(self.ids)}
        self.hash_of = dict(zip(self.ids, hashes)) if hashes is not None else None

    def _collection_rows(self, collection: str) -> tuple[list[int], np.ndarray]:
        rows_db = self.conn.execute(
            "SELECT id, content_hash FROM chunks WHERE collection = ? ORDER BY id",
            (collection,),
        ).fetchall()
        chunk_ids = [r["id"] for r in rows_db]
        if not chunk_ids:
            raise EmptyIndexError(collection)
        missing = [cid for cid in chunk_ids if cid not in self.row_of]
        if missing:
            raise StoreOutOfSync(
                f"Collection '{collection}': {len(missing)} chunks have no embedding. "
                f"The store is out of sync - run: brain index --collection {collection}",
                collection=collection,
            )
        if self.hash_of is not None:
            # A vector whose hash no longer matches its chunk belongs to text
            # that was replaced (SQLite reuses freed rowids). Answering from
            # it would cite the wrong source with full confidence.
            stale = [r["id"] for r in rows_db
                     if self.hash_of.get(r["id"]) != r["content_hash"]]
            if stale:
                raise StoreOutOfSync(
                    f"Collection '{collection}': {len(stale)} stored vector(s) no longer "
                    f"match their chunk text, so results would cite the wrong sources. "
                    f"Run: brain index --collection {collection}"
                )
        rows = np.array([self.row_of[cid] for cid in chunk_ids])
        return chunk_ids, self.matrix[rows]

    def search_collection(
        self, query: str, collection: str, k: int, *, floor: float | None = None
    ) -> list[Hit]:
        """Top-k chunks in one collection scoring at or above the floor.
        Raises EmptyIndexError if the collection has no chunks at all."""
        if floor is None:
            floor = self.config.settings.similarity_floor
        if k <= 0:
            raise BrainError(f"k must be a positive integer, got {k}")
        chunk_ids, sub = self._collection_rows(collection)
        qvec = self.embedder.embed_query(query)
        scores = sub @ qvec
        k = min(k, len(chunk_ids))
        top = np.argsort(scores)[::-1][:k]
        hits: list[Hit] = []
        keep = [int(i) for i in top if float(scores[i]) >= floor]
        if keep:
            id_list = [chunk_ids[i] for i in keep]
            rows = {
                r["id"]: r for r in self.conn.execute(
                    f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(id_list))})",
                    id_list,
                )
            }
            for i in keep:
                r = rows[chunk_ids[i]]
                hits.append(Hit(
                    chunk_id=r["id"], collection=r["collection"],
                    source_path=r["source_path"], locator=r["locator"],
                    text=r["text"], score=float(scores[i]),
                ))
        return hits

    def search_global(
        self, query: str, k_per_collection: int, *, floor: float | None = None
    ) -> dict[str, list[Hit]]:
        """Top-k per collection, separately. Collections with an empty index
        are skipped (reported via the 'empty' key callers can query with
        empty_collections()); a fully empty global index raises."""
        results: dict[str, list[Hit]] = {}
        any_indexed = False
        for col in self.config.collections:
            try:
                hits = self.search_collection(query, col.name, k_per_collection, floor=floor)
                any_indexed = True
                if hits:
                    results[col.name] = hits
            except EmptyIndexError:
                continue
        if not any_indexed:
            raise EmptyIndexError("all")
        return results

    def empty_collections(self) -> list[str]:
        """Names of configured collections with zero indexed chunks."""
        counts = {
            r["collection"]: r["n"] for r in self.conn.execute(
                "SELECT collection, COUNT(*) AS n FROM chunks GROUP BY collection"
            )
        }
        return [c.name for c in self.config.collections if counts.get(c.name, 0) == 0]
