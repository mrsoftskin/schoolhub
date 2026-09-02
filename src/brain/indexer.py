"""Incremental indexing: scan collection roots, chunk changed files, embed
new chunks, and keep the embedding store aligned with the chunks table.

Change detection is mtime + size per file; --force reindexes everything.
Parse failures never abort a run - they are collected, persisted to
index_status.failures_json, and reported at the end (fail loud, finish work).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Callable

import numpy as np

from .chunking import chunk_file
from .config import Collection, Config
from .embeddings import Embedder, EmbeddingStore
from .errors import BrainError, ParseError, StoreOutOfSync


@dataclass
class FileFailure:
    path: str
    reason: str


@dataclass
class CollectionReport:
    collection: str
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    chunks_added: int = 0
    failures: list[FileFailure] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)


@dataclass
class IndexReport:
    collections: list[CollectionReport] = field(default_factory=list)

    @property
    def total_failures(self) -> list[FileFailure]:
        return [f for c in self.collections for f in c.failures]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def discover_files(col: Collection) -> tuple[list[Path], list[str]]:
    """All files under the collection roots matching include minus exclude.
    Returns (files, missing_roots). Glob patterns match the path relative to
    the root, using forward slashes."""
    missing = [str(r) for r in col.roots if not r.exists()]
    files: list[Path] = []
    seen: set[Path] = set()
    for root in col.roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p in seen:
                continue
            rel = p.relative_to(root).as_posix()
            if not any(_glob_match(rel, g) for g in col.include):
                continue
            if any(_glob_match(rel, g) for g in col.exclude):
                continue
            seen.add(p)
            files.append(p)
    return files, missing


def _glob_match(rel: str, pattern: str) -> bool:
    """fnmatch with '**/' also matching zero directories, so '**/*.md'
    matches a file at the root of the collection.

    Case-insensitive: a "Lecture.PDF" must index exactly like "lecture.pdf".
    fnmatch.fnmatch normalizes case only on case-insensitive platforms, so on
    macOS/Linux an upper-case extension silently fell out of the include list
    (chunking.py already lowercases the suffix, so the rest of the pipeline
    was ready for it).
    """
    rel_l, pat_l = rel.lower(), pattern.lower()
    if fnmatch.fnmatchcase(rel_l, pat_l):
        return True
    if pat_l.startswith("**/") and fnmatch.fnmatchcase(rel_l, pat_l[3:]):
        return True
    return False


# How many chunks to embed between durable writes of the embedding store.
# Proportional by design: a small library finishes inside one checkpoint
# window anyway, while a multi-thousand-chunk library (the case where an
# interrupted run actually hurts) saves progress repeatedly.
EMBED_CHECKPOINT_CHUNKS = 2560


def index_collections(
    config: Config,
    conn: Connection,
    embedder: Embedder,
    *,
    only: list[str] | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> IndexReport:
    """Index the named collections (default: all). Returns a full report."""
    names = only or config.collection_names()
    targets = [config.collection(n) for n in names]

    say = progress or (lambda _msg: None)
    report = IndexReport()
    new_chunk_texts: dict[int, str] = {}  # chunk id -> text, pending embedding

    # Pack chunks to what the embedding model will actually read. Sizing by a
    # chars/4 estimate overflows the model's window and silently discards the
    # tail of every long chunk (measured: 20% of all indexed text).
    count_tokens = getattr(embedder, "count_tokens", None)
    target_tokens = None
    if count_tokens is not None:
        try:
            target_tokens = int(embedder.max_tokens)
        except Exception as e:  # a stub embedder without a real model
            say(f"Could not read the embedder's token limit ({e}); "
                f"falling back to the estimated chunk target.")
            count_tokens = None
    if target_tokens:
        say(f"Chunk target: {target_tokens} tokens (the model's usable window).")

    for col in targets:
        crep = CollectionReport(collection=col.name)
        report.collections.append(crep)
        files, missing = discover_files(col)
        crep.missing_roots = missing
        for m in missing:
            crep.failures.append(FileFailure(path=m, reason="root path does not exist"))
        crep.scanned = len(files)
        say(f"[{col.name}] {len(files)} candidate files" + (f", {len(missing)} missing roots" if missing else ""))

        on_disk = {str(p) for p in files}
        known = {
            row["path"]: (row["mtime"], row["size"])
            for row in conn.execute(
                "SELECT path, mtime, size FROM files WHERE collection = ?", (col.name,)
            )
        }

        # Remove records for files that disappeared from disk - but ONLY when
        # every root was readable. An unplugged drive or a syncing cloud
        # folder makes discover_files() return nothing, and deleting on that
        # basis would wipe the whole collection's index over a transient blip.
        if missing:
            say(f"[{col.name}] {len(missing)} root(s) unavailable - keeping existing "
                f"entries instead of treating their files as deleted")
        else:
            for gone in set(known) - on_disk:
                conn.execute("DELETE FROM chunks WHERE source_path = ? AND collection = ?", (gone, col.name))
                conn.execute("DELETE FROM files WHERE path = ?", (gone,))
                crep.removed += 1

        for path in files:
            spath = str(path)
            try:
                stat = path.stat()
            except OSError as e:
                # One unreadable file must not abort the whole run.
                crep.failures.append(FileFailure(path=spath, reason=f"stat failed: {e}"))
                continue
            if not force and spath in known:
                old_mtime, old_size = known[spath]
                if abs(old_mtime - stat.st_mtime) < 1e-6 and old_size == stat.st_size:
                    crep.skipped += 1
                    continue
            try:
                chunks = chunk_file(path, count_tokens=count_tokens, target_tokens=target_tokens)
            except ParseError as e:
                crep.failures.append(FileFailure(path=spath, reason=e.reason))
                continue

            if not chunks:
                # Extracting nothing from a file we were asked to index is a
                # failure, not a success: recording it as indexed would hide
                # the file forever behind the mtime/size skip.
                crep.failures.append(FileFailure(
                    path=spath,
                    reason="parsed to zero chunks - no extractable text "
                           "(scanned images, an empty file, or an unsupported layout)",
                ))
                continue

            conn.execute("DELETE FROM chunks WHERE source_path = ? AND collection = ?", (spath, col.name))
            for ch in chunks:
                cur = conn.execute(
                    "INSERT INTO chunks (collection, source_path, locator, text, content_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (col.name, spath, ch.locator, ch.text, ch.content_hash),
                )
                new_chunk_texts[cur.lastrowid] = ch.text
            conn.execute(
                "INSERT OR REPLACE INTO files (path, collection, mtime, size, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (spath, col.name, stat.st_mtime, stat.st_size, _now()),
            )
            crep.indexed += 1
            crep.chunks_added += len(chunks)
            say(f"[{col.name}] indexed {path.name} ({len(chunks)} chunks)")

        doc_count = conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE collection = ?", (col.name,)
        ).fetchone()["n"]
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE collection = ?", (col.name,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT OR REPLACE INTO index_status "
            "(collection, last_indexed, doc_count, chunk_count, failures_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                col.name, _now(), doc_count, chunk_count,
                json.dumps([{"path": f.path, "reason": f.reason} for f in crep.failures]),
            ),
        )
        # Commit per collection rather than holding the write lock for the
        # whole run: a full index takes minutes, and the web app has to be
        # able to save a chat message while it runs.
        conn.commit()

    conn.commit()
    _sync_embedding_store(config, conn, embedder, new_chunk_texts, say)
    return report


def _sync_embedding_store(
    config: Config,
    conn: Connection,
    embedder: Embedder,
    new_chunk_texts: dict[int, str],
    say: Callable[[str], None],
) -> None:
    """Rebuild the store so it holds exactly the chunk ids in the DB, reusing
    existing rows and embedding whatever is missing or stale (self-healing).

    Staleness matters because chunks.id is a plain SQLite rowid: deleting a
    file's chunks frees ids that the next insert reuses. If a run is killed
    between the DB commit and the store save, a reused id can end up pointing
    at the vector of the text it replaced - a silent mismatch that returns
    confident answers citing the wrong file. Comparing the stored content
    hash against the DB's detects exactly that, and re-embeds those rows.
    """
    store = EmbeddingStore(config.settings.data_dir, config.settings.embedding_model)
    try:
        old_ids, old_matrix, old_hashes = store.load()
    except (StoreOutOfSync, ValueError, OSError) as e:
        # Indexing is the recovery path for a corrupt or stale-model store:
        # say so plainly and rebuild every vector rather than refusing.
        say(f"Rebuilding the embedding store from scratch: {e}")
        old_ids, old_matrix, old_hashes = [], np.zeros((0, 0), dtype=np.float32), None
    old_row: dict[int, int] = {cid: i for i, cid in enumerate(old_ids)}
    old_hash: dict[int, str] = (
        dict(zip(old_ids, old_hashes)) if old_hashes is not None else {}
    )

    db_rows = conn.execute("SELECT id, content_hash FROM chunks ORDER BY id").fetchall()
    db_ids = [r["id"] for r in db_rows]
    db_hash = {r["id"]: r["content_hash"] for r in db_rows}

    missing = [cid for cid in db_ids if cid not in old_row and cid not in new_chunk_texts]
    if old_hashes is None:
        # Legacy manifest with no hashes: nothing can be verified, so re-embed
        # everything once and stamp hashes on the way out.
        stale = [cid for cid in db_ids if cid in old_row and cid not in new_chunk_texts]
        if stale:
            say(f"Embedding store predates content hashing; re-embedding "
                f"{len(stale)} chunks once to make it verifiable.")
    else:
        stale = [
            cid for cid in db_ids
            if cid in old_row and cid not in new_chunk_texts
            and old_hash.get(cid) != db_hash[cid]
        ]
        if stale:
            say(f"{len(stale)} stored vector(s) no longer match their chunk text "
                f"(an interrupted run reused those ids); re-embedding them.")
    if missing:
        say(f"Embedding store is missing {len(missing)} existing chunks; re-embedding them.")

    needs_text = missing + stale
    if needs_text:
        # Batched: a big backlog would exceed SQLite's bound-variable limit.
        for start in range(0, len(needs_text), 500):
            batch = needs_text[start:start + 500]
            for row in conn.execute(
                f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(batch))})",
                batch,
            ):
                new_chunk_texts[row["id"]] = row["text"]

    to_embed = [cid for cid in db_ids if cid in new_chunk_texts]
    embedded: dict[int, np.ndarray] = {}

    def _write_store(ids_wanted: list[int]) -> int:
        """Persist vectors for whichever of `ids_wanted` we actually hold.

        Each row is saved with the hash of the text its vector was built
        from - the NEW hash for something just embedded, the OLD one for a
        vector carried over. Writing db_hash for a carried-over row would
        mark a stale vector as current and defeat the drift detection above.
        """
        have = [c for c in ids_wanted if c in embedded or c in old_row]
        if not have:
            return 0
        rows = [embedded[c] if c in embedded else old_matrix[old_row[c]]
                for c in have]
        hashes = [db_hash[c] if c in embedded else old_hash.get(c, "")
                  for c in have]
        store.save(have, np.vstack(rows).astype(np.float32), hashes)
        return len(have)

    if to_embed:
        say(f"Embedding {len(to_embed)} chunks with {config.settings.embedding_model} (CPU)...")
        BATCH = 128
        # Checkpoint periodically. Embedding a big library takes minutes, and
        # this runs in a background thread at app launch - if the user closes
        # the window (or the machine sleeps) before the single end-of-run save,
        # EVERY vector is lost while the chunks stay committed, leaving a
        # library that indexes fine and matches nothing. That is exactly what
        # the first friend to install this hit. A checkpoint makes the work
        # durable: a killed run now costs at most the last few batches, and the
        # partial store is self-healing because the missing ids are re-embedded
        # on the next run.
        since_checkpoint = 0
        for start in range(0, len(to_embed), BATCH):
            batch_ids = to_embed[start:start + BATCH]
            vecs = embedder.embed_docs([new_chunk_texts[cid] for cid in batch_ids])
            for cid, vec in zip(batch_ids, vecs):
                embedded[cid] = vec
            done = min(start + BATCH, len(to_embed))
            say(f"  embedded {done}/{len(to_embed)}")
            since_checkpoint += len(batch_ids)
            if since_checkpoint >= EMBED_CHECKPOINT_CHUNKS and done < len(to_embed):
                kept = _write_store(db_ids)
                since_checkpoint = 0
                say(f"  saved progress ({kept} vectors on disk)")

    if not db_ids:
        store.save([], np.zeros((0, 0), dtype=np.float32), [])
        return

    dim = old_matrix.shape[1] if old_matrix.size else next(iter(embedded.values())).shape[0]
    rows: list[np.ndarray] = []
    for cid in db_ids:
        if cid in embedded:
            rows.append(embedded[cid])
        elif cid in old_row:
            rows.append(old_matrix[old_row[cid]])
        else:  # unreachable by construction, but fail loud rather than misalign
            raise BrainError(f"No embedding available for chunk {cid}")
    store.save(db_ids, np.vstack(rows).astype(np.float32), [db_hash[c] for c in db_ids])
    say(f"Embedding store: {len(db_ids)} vectors, dim {dim}.")
