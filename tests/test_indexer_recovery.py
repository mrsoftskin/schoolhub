"""Interrupted indexing must never leave a library that matches nothing.

The first friend to install this ended up with chunks committed to the DB and
ZERO vectors on disk: search returned nothing while the Library tab looked
fully populated. Cause: chunk rows commit per collection as they go, but the
embedding store used to be written exactly once, after every collection had
been chunked. Anything that killed the run before that single save - closing
the launcher window, sleep, a crash - discarded every vector.
"""

from __future__ import annotations

import numpy as np
import pytest

from brain.embeddings import EmbeddingStore
from conftest import add_doc, make_core


class _Boom(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _small_checkpoint(monkeypatch):
    """Checkpoint every 8 chunks so a small fixture exercises the same code
    path a 38,000-chunk library hits in production."""
    from brain import indexer

    monkeypatch.setattr(indexer, "EMBED_CHECKPOINT_CHUNKS", 8)


class FlakyEmbedder:
    """Embeds deterministically, then dies partway - a killed run, in-process."""

    def __init__(self, die_after: int | None = None):
        self.die_after = die_after
        self.embedded = 0

    @property
    def max_tokens(self) -> int:
        return 510

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _vec(self, text: str):
        # Deterministic per text, so two independent runs must agree exactly.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.random(8).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed_docs(self, texts):
        if self.die_after is not None and self.embedded >= self.die_after:
            raise _Boom("interrupted")
        self.embedded += len(texts)
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def _library(tmp_path, n_files=300):
    """Enough files to span several embed batches (BATCH=128)."""
    core = make_core(tmp_path, [{"name": "FINC389", "assist_level": "full"}])
    for i in range(n_files):
        add_doc(tmp_path, "FINC389", f"doc{i:03d}.md",
                f"# Chapter {i}\n\nAmortization schedule detail {i}.\n")
    return core


def _store(core) -> EmbeddingStore:
    return EmbeddingStore(core.config.settings.data_dir,
                          core.config.settings.embedding_model)


def _stored(core):
    try:
        return _store(core).load()
    except Exception:
        return [], np.zeros((0, 0), dtype=np.float32), []


def _db_hashes(core) -> dict[int, str]:
    conn = core.open_db()
    try:
        return {r["id"]: r["content_hash"]
                for r in conn.execute("SELECT id, content_hash FROM chunks")}
    finally:
        conn.close()


def test_interrupted_index_persists_partial_progress(tmp_path):
    """The bug: a killed run left ZERO vectors behind for every chunk."""
    core = _library(tmp_path)
    core.embedder = FlakyEmbedder(die_after=200)
    with pytest.raises(_Boom):
        core.index()

    assert _db_hashes(core), "chunk rows should be committed as the run goes"
    ids, matrix, _hashes = _stored(core)
    assert len(ids) > 0, (
        "an interrupted run persisted no vectors at all - the library would "
        "index fine and match nothing, which is what the first friend hit"
    )
    assert matrix.shape[0] == len(ids)


def test_rerun_after_interruption_heals_completely(tmp_path):
    """The next run must FINISH the job. The files are unchanged, so mtime and
    size mark them 'skipped' - only the store's own missing-id detection can
    recover the chunks whose vectors were lost."""
    core = _library(tmp_path)
    core.embedder = FlakyEmbedder(die_after=200)
    with pytest.raises(_Boom):
        core.index()

    core.embedder = FlakyEmbedder()          # healthy run
    core.index()

    db_hash = _db_hashes(core)
    ids, matrix, hashes = _stored(core)
    assert sorted(ids) == sorted(db_hash), "every chunk must end up with a vector"
    assert matrix.shape[0] == len(ids)
    assert all(h == db_hash[i] for i, h in zip(ids, hashes)), \
        "a stored hash disagreeing with its chunk means a mismatched vector"


def test_recovered_store_matches_a_clean_run_exactly(tmp_path):
    """The strongest statement of correctness: interrupt-then-heal must land
    on byte-identical vectors to an uninterrupted run.

    This is what catches a carried-over vector being stamped with the chunk's
    CURRENT hash instead of the hash it was built from - that row would look
    current, never get re-embedded, and quietly keep the wrong vector.
    """
    clean = _library(tmp_path / "clean")
    clean.embedder = FlakyEmbedder()
    clean.index()
    clean_ids, clean_matrix, _ = _stored(clean)
    clean_by_hash = {h: clean_matrix[i] for i, h in
                     enumerate(_db_hashes(clean)[c] for c in clean_ids)}

    messy = _library(tmp_path / "messy")
    messy.embedder = FlakyEmbedder(die_after=200)
    with pytest.raises(_Boom):
        messy.index()
    messy.embedder = FlakyEmbedder()
    messy.index()

    messy_hash = _db_hashes(messy)
    messy_ids, messy_matrix, _ = _stored(messy)
    assert len(messy_ids) == len(clean_ids)
    for row, cid in enumerate(messy_ids):
        expected = clean_by_hash[messy_hash[cid]]
        assert np.allclose(messy_matrix[row], expected), (
            f"chunk {cid} recovered a vector that differs from a clean run")


def test_edited_file_after_an_interruption_is_not_left_stale(tmp_path):
    """A file edited between a killed run and the retry must end up with a
    vector of its NEW text, not a carried-over one."""
    core = _library(tmp_path, n_files=300)
    core.embedder = FlakyEmbedder(die_after=200)
    with pytest.raises(_Boom):
        core.index()

    add_doc(tmp_path, "FINC389", "doc000.md",
            "# Chapter 0\n\nCompletely rewritten content about duration risk.\n")
    core.embedder = FlakyEmbedder()
    core.index()

    db_hash = _db_hashes(core)
    ids, matrix, hashes = _stored(core)
    assert sorted(ids) == sorted(db_hash)
    assert all(h == db_hash[i] for i, h in zip(ids, hashes))

    # The rewritten text must be retrievable, which it cannot be if its vector
    # was carried over from the old text.
    conn = core.open_db()
    try:
        row = conn.execute(
            "SELECT id, text FROM chunks WHERE text LIKE '%duration risk%'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the edited file should have been re-chunked"
    stored_vec = matrix[list(ids).index(row["id"])]
    assert np.allclose(stored_vec, core.embedder.embed_query(row["text"]))
