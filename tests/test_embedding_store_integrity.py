"""The embedding store must never serve vectors that do not match the manifest.

A crash between the two renames in save() can leave a NEW matrix under an OLD
manifest. The row-count check cannot see that when the counts coincide, and
neither can the per-row hashes - they come from the same stale manifest, so
they validate against themselves. A fingerprint of the matrix, carried in the
manifest, is what makes the pair verifiable rather than merely ordered.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from brain.embeddings import EmbeddingStore
from brain.errors import StoreOutOfSync

MODEL = "test-model"


def _store(tmp_path):
    return EmbeddingStore(tmp_path, MODEL)


def _vec(seed, dim=8):
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def test_round_trip(tmp_path):
    s = _store(tmp_path)
    m = np.vstack([_vec(1), _vec(2)])
    s.save([10, 11], m, ["h1", "h2"])
    ids, matrix, hashes = s.load()
    assert ids == [10, 11] and hashes == ["h1", "h2"]
    assert np.allclose(matrix, m)


def test_a_new_matrix_under_an_old_manifest_is_detected(tmp_path):
    """The exact interrupted-save state, with the row count UNCHANGED so the
    shape check cannot catch it."""
    s = _store(tmp_path)
    s.save([10, 11], np.vstack([_vec(1), _vec(2)]), ["h1", "h2"])
    manifest = s.manifest_path.read_text(encoding="utf-8")

    # Same ids, same row count, different vectors - then restore the manifest,
    # simulating a crash after the matrix rename but before the manifest one.
    s.save([10, 11], np.vstack([_vec(99), _vec(98)]), ["h1", "h2"])
    s.manifest_path.write_text(manifest, encoding="utf-8")

    with pytest.raises(StoreOutOfSync) as e:
        s.load()
    assert "interrupted save" in str(e.value)


def test_a_truncated_matrix_is_reported_not_crashed(tmp_path):
    s = _store(tmp_path)
    s.save([1], np.vstack([_vec(1)]), ["h"])
    data = s.matrix_path.read_bytes()
    s.matrix_path.write_bytes(data[: len(data) // 2])
    with pytest.raises((StoreOutOfSync, ValueError, OSError)):
        s.load()


def test_a_legacy_manifest_without_a_fingerprint_still_loads(tmp_path):
    """Existing installs must not be forced into a rebuild by the upgrade."""
    s = _store(tmp_path)
    s.save([1, 2], np.vstack([_vec(1), _vec(2)]), ["a", "b"])
    raw = json.loads(s.manifest_path.read_text(encoding="utf-8"))
    del raw["matrix_sha256"]
    s.manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    ids, matrix, hashes = s.load()
    assert ids == [1, 2] and matrix.shape[0] == 2


def test_the_manifest_records_the_matrix_fingerprint(tmp_path):
    s = _store(tmp_path)
    s.save([1], np.vstack([_vec(1)]), ["a"])
    raw = json.loads(s.manifest_path.read_text(encoding="utf-8"))
    assert len(raw["matrix_sha256"]) == 64


def test_an_interrupted_store_rebuilds_instead_of_breaking_index(tmp_path):
    """`brain index` is the documented recovery path, so it must survive a
    store it cannot read rather than raising at the user."""
    from conftest import add_doc, make_core

    core = make_core(tmp_path, [{"name": "C", "assist_level": "full"}])
    add_doc(tmp_path, "C", "a.md", "amortization schedule detail")
    core.index()
    store = EmbeddingStore(core.config.settings.data_dir,
                           core.config.settings.embedding_model)
    store.matrix_path.write_bytes(b"garbage not a numpy file")
    core.index(force=True)          # must not raise
    ids, matrix, _ = store.load()
    assert len(ids) == matrix.shape[0] > 0
