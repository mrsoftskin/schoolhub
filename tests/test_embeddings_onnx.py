"""The ONNX embedder: the default backend.

It replaced sentence-transformers/PyTorch after verifying byte-identical
output (cosine 1.000000, max abs diff 1.5e-7 across docs and queries, and
identical count_tokens including past the 512-token window). That parity is
why swapping it in did NOT invalidate existing indexes or the calibrated
similarity floor. Torch is no longer installed, so the comparison cannot run
here; these tests pin the properties the rest of the app relies on.
"""

from __future__ import annotations

import numpy as np
import pytest

from brain.embeddings import BGE_QUERY_PREFIX, OnnxBgeEmbedder


def _model_cached() -> bool:
    """Only run model-loading tests when the weights are already cached, so a
    plain `pytest` on a fresh machine never pulls 130 MB."""
    from pathlib import Path

    hub = Path.home() / ".cache" / "huggingface" / "hub"
    return any(hub.glob("models--BAAI--bge-small-en-v1.5")) if hub.exists() else False


needs_model = pytest.mark.skipif(not _model_cached(),
                                 reason="bge model not cached locally")


def test_window_leaves_room_for_special_tokens():
    """512-token window minus [CLS] and [SEP]. chunking packs to this, so an
    off-by-two silently drops the tail of every chunk."""
    assert OnnxBgeEmbedder().max_tokens == 510


def test_empty_batch_needs_no_model():
    """Indexing a collection with nothing new must not load the model."""
    e = OnnxBgeEmbedder()
    out = e.embed_docs([])
    assert out.shape == (0, 384)
    assert e._session is None          # never loaded


@needs_model
def test_vectors_are_normalized_and_deterministic():
    e = OnnxBgeEmbedder()
    v = e.embed_docs(["Attendance is 10 percent of the grade.",
                      "The Fed sets monetary policy."])
    assert v.shape == (2, 384) and v.dtype == np.float32
    # L2-normalized, so cosine similarity is a plain dot product.
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert np.allclose(v, e.embed_docs(["Attendance is 10 percent of the grade.",
                                        "The Fed sets monetary policy."]))


@needs_model
def test_query_uses_the_bge_retrieval_prefix():
    """bge is trained with a query-side instruction; embedding a query must
    not be the same as embedding it as a document."""
    e = OnnxBgeEmbedder()
    q = e.embed_query("attendance policy")
    as_doc = e.embed_docs(["attendance policy"])[0]
    as_prefixed = e.embed_docs([BGE_QUERY_PREFIX + "attendance policy"])[0]
    assert float(np.dot(q, as_prefixed)) > 0.999
    assert float(np.dot(q, as_doc)) < 0.999


@needs_model
def test_count_tokens_is_not_truncated_by_the_window():
    """The counter must report the TRUE length past 512 - chunking splits on
    it. A truncating tokenizer would report 512 and hide the overflow."""
    e = OnnxBgeEmbedder()
    assert e.count_tokens("") == 0
    long_text = "The quick brown fox. " * 200
    assert e.count_tokens(long_text) > 900


@needs_model
def test_batching_matches_single_pass():
    """Batching is an implementation detail; results must not depend on it."""
    e = OnnxBgeEmbedder()
    texts = [f"Chapter {i} covers interest rates." for i in range(e.BATCH + 5)]
    batched = e.embed_docs(texts)
    one_at_a_time = np.vstack([e.embed_docs([t]) for t in texts])
    assert np.allclose(batched, one_at_a_time, atol=1e-5)
