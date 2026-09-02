"""Embeddings: a small Embedder protocol, the real sentence-transformers
implementation (lazy-loaded), and the on-disk store.

The store is a numpy float32 matrix (embeddings.npy) plus an id-order manifest
(embeddings_ids.json). Row i of the matrix is the embedding of chunk id
manifest[i]. All vectors are L2-normalized so cosine similarity is a dot
product.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

from .errors import StoreOutOfSync

MATRIX_FILE = "embeddings.npy"
MANIFEST_FILE = "embeddings_ids.json"

# bge models are trained with this query-side instruction for retrieval.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    def embed_docs(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...
    def count_tokens(self, text: str) -> int: ...
    @property
    def max_tokens(self) -> int: ...


class OnnxBgeEmbedder:
    """BAAI/bge-small-en-v1.5 via ONNX Runtime - the default embedder.

    Produces byte-identical vectors to the sentence-transformers/PyTorch path
    (verified: cosine 1.000000, max abs diff 0.0), so an index built by either
    backend stays valid and the calibrated similarity floor still applies. It
    is chosen as the default because it drops the PyTorch dependency entirely:
      * ~350 MB of wheels becomes ~60 MB, and model load is near-instant;
      * Intel Macs work again (torch has shipped no macOS x86_64 build since
        2.2.2, which was the sole reason older Macs were unsupportable).

    Weights come from the model's own repo (onnx/model.onnx), so this is the
    same trained model, not a re-export.
    """

    # BGE-small's window. Two slots are reserved for [CLS] and [SEP].
    MAX_SEQ = 512
    BATCH = 32

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        self._session = None
        self._tok = None          # truncating+padding, for encoding
        self._counter = None      # raw, for honest token counts
        self._input_names: set[str] = set()
        self._lock = threading.Lock()

    def _load(self):
        if self._session is None:
            with self._lock:
                if self._session is None:
                    import onnxruntime as ort
                    from huggingface_hub import hf_hub_download
                    from tokenizers import Tokenizer

                    onnx_path = hf_hub_download(self.model_name, "onnx/model.onnx")
                    tok_path = hf_hub_download(self.model_name, "tokenizer.json")
                    # Thread count is worth setting explicitly. Measured on a
                    # 16-logical-core machine: the default managed ~41
                    # chunks/sec, 8 threads ~50, and 16 threads only ~24 -
                    # asking for every logical core makes it SLOWER, because
                    # hyperthread siblings fight over the same vector units.
                    # Half the logical count approximates the physical cores;
                    # the cap keeps a big workstation from oversubscribing.
                    so = ort.SessionOptions()
                    so.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 2) // 2))
                    sess = ort.InferenceSession(
                        onnx_path, so, providers=["CPUExecutionProvider"])

                    tok = Tokenizer.from_file(tok_path)
                    tok.enable_truncation(max_length=self.MAX_SEQ)
                    tok.enable_padding()
                    # A SECOND, unconfigured tokenizer for counting: truncation
                    # is global state on a Tokenizer, and a truncating counter
                    # would report 512 for everything longer, hiding exactly the
                    # overflow that chunking exists to prevent.
                    self._counter = Tokenizer.from_file(tok_path)
                    self._tok = tok
                    self._input_names = {i.name for i in sess.get_inputs()}
                    self._session = sess
        return self._session

    @property
    def max_tokens(self) -> int:
        """Usable content tokens per embedding, leaving room for [CLS]/[SEP]."""
        return self.MAX_SEQ - 2

    def count_tokens(self, text: str) -> int:
        self._load()
        return len(self._counter.encode(text, add_special_tokens=False).ids)

    def _encode(self, texts: list[str]) -> np.ndarray:
        sess = self._load()
        out = []
        for i in range(0, len(texts), self.BATCH):
            batch = texts[i:i + self.BATCH]
            encs = self._tok.encode_batch(batch)
            feed = {
                "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs],
                                           dtype=np.int64),
            }
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.array(
                    [e.type_ids for e in encs], dtype=np.int64)
            hidden = sess.run(None, feed)[0]
            # BGE pools the [CLS] token, then L2-normalizes so cosine
            # similarity is a plain dot product.
            cls = hidden[:, 0, :]
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            out.append(cls / np.maximum(norms, 1e-12))
        return np.vstack(out).astype(np.float32)

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        return self._encode(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([BGE_QUERY_PREFIX + text])[0]


class BgeEmbedder:
    """BAAI/bge-small-en-v1.5 on local CPU. The model (~130 MB) is downloaded
    by sentence-transformers on first use.

    Exposes the real tokenizer so chunking can pack to what the model will
    actually read. This matters: the model's window is 512 tokens, and a
    chars/4 estimate underestimates real tokens by ~1.45x on this content
    (5x worst case), so estimate-sized chunks silently overflow and the
    tail is dropped before it is ever embedded.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        # Guarded: concurrent web requests would otherwise each build their
        # own SentenceTransformer (~130 MB and seconds of CPU apiece).
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    @property
    def max_tokens(self) -> int:
        """Usable content tokens per embedding, leaving room for [CLS]/[SEP]."""
        return int(self._load().max_seq_length) - 2

    def count_tokens(self, text: str) -> int:
        # verbose=False: this is a measurement, and chunking deliberately
        # measures text longer than the window before splitting it. Without
        # this, every index run prints "sequence longer than maximum" warnings
        # that look like errors but describe input we are about to split.
        return len(self._load().tokenizer(
            text, add_special_tokens=False, verbose=False)["input_ids"])

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        model = self._load()
        vecs = model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False,
            batch_size=32,
        )
        return np.asarray(vecs, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        model = self._load()
        vec = model.encode(
            [BGE_QUERY_PREFIX + text], normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vec, dtype=np.float32)[0]


class EmbeddingStore:
    """Aligned matrix + manifest on disk, atomically rewritten on save.

    The manifest records which model produced the vectors. Changing
    settings.embedding_model therefore fails loudly on the next search
    instead of silently comparing vectors from two different embedding
    spaces (which yields plausible-looking but meaningless rankings).
    """

    def __init__(self, data_dir: str | Path, model_name: str | None = None):
        self.data_dir = Path(data_dir)
        self.model_name = model_name
        self.matrix_path = self.data_dir / MATRIX_FILE
        self.manifest_path = self.data_dir / MANIFEST_FILE

    def exists(self) -> bool:
        return self.matrix_path.exists() and self.manifest_path.exists()

    def load(self) -> tuple[list[int], np.ndarray, list[str] | None]:
        """Returns (chunk ids in row order, matrix, per-row content hashes).

        Hashes are None for legacy manifests written before they were
        recorded. Empty store -> ([], (0,0), None).
        """
        if not self.exists():
            return [], np.zeros((0, 0), dtype=np.float32), None
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        # v1 manifests were a bare id list; v2 added the model name; v3 adds
        # content hashes so a row can be checked against the chunk it claims.
        if isinstance(raw, list):
            ids, stored_model, hashes = raw, None, None
        else:
            ids = raw["ids"]
            stored_model = raw.get("model")
            hashes = raw.get("hashes")
        matrix = np.load(self.matrix_path)
        # Verify the pair, not just the shapes: a crash between the two
        # renames can leave a new matrix under an old manifest, which the
        # row-count check below cannot detect when the counts coincide.
        want = raw.get("matrix_sha256") if isinstance(raw, dict) else None
        if want:
            import hashlib

            got = hashlib.sha256(self.matrix_path.read_bytes()).hexdigest()
            if got != want:
                raise StoreOutOfSync(
                    "Embedding store is corrupt: the vectors do not match the "
                    "manifest that describes them (an interrupted save). "
                    "Re-run: brain index"
                )
        if len(ids) != matrix.shape[0]:
            raise StoreOutOfSync(
                f"Embedding store is corrupt: manifest has {len(ids)} ids but "
                f"matrix has {matrix.shape[0]} rows. Re-run: brain index"
            )
        if hashes is not None and len(hashes) != len(ids):
            raise StoreOutOfSync(
                f"Embedding store is corrupt: {len(hashes)} hashes for "
                f"{len(ids)} ids. Re-run: brain index"
            )
        if self.model_name and stored_model and stored_model != self.model_name:
            raise StoreOutOfSync(
                f"Embedding store was built with '{stored_model}' but config says "
                f"'{self.model_name}'. Vectors from different models are not "
                f"comparable. Re-run: brain index"
            )
        return ids, matrix, hashes

    def save(self, ids: list[int], matrix: np.ndarray, hashes: list[str]) -> None:
        """Write the store. The manifest is renamed LAST and carries the row
        count and per-row hashes, so a crash between the two renames leaves a
        state that load() detects rather than one that silently mismatches."""
        if matrix.shape[0] != len(ids):
            raise ValueError(f"ids ({len(ids)}) and matrix rows ({matrix.shape[0]}) mismatch")
        if len(hashes) != len(ids):
            raise ValueError(f"ids ({len(ids)}) and hashes ({len(hashes)}) mismatch")
        import hashlib
        import os

        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp_matrix = self.matrix_path.with_suffix(".npy.tmp")
        tmp_manifest = self.manifest_path.with_suffix(".json.tmp")
        # np.save appends '.npy' to bare paths; write through a handle to
        # keep the temp filename exact for the atomic replace below.
        # fsync before renaming: without it a power loss can commit the
        # rename while the blocks are still buffered, leaving a truncated
        # file that is neither the old store nor the new one.
        with open(tmp_matrix, "wb") as f:
            np.save(f, matrix.astype(np.float32, copy=False))
            f.flush()
            os.fsync(f.fileno())
        # A fingerprint of the matrix, carried in the manifest, is what makes
        # the pair verifiable rather than merely ordered. The row count alone
        # cannot see a NEW matrix left under an OLD manifest when the counts
        # happen to match - and the per-row hashes cannot either, because
        # they come from that same stale manifest and would validate against
        # themselves.
        digest = hashlib.sha256(tmp_matrix.read_bytes()).hexdigest()
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump({"model": self.model_name, "ids": ids, "hashes": hashes,
                       "matrix_sha256": digest}, f)
            f.flush()
            os.fsync(f.fileno())
        tmp_matrix.replace(self.matrix_path)
        tmp_manifest.replace(self.manifest_path)
