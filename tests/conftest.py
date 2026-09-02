"""Shared test fixtures: a deterministic fake embedder and a project factory
that builds a real config.toml + document tree in tmp_path.

The fake embedder hashes words into buckets, so texts sharing words are
similar and disjoint texts are near-orthogonal - enough to exercise retrieval,
the floor, and global mode without loading a real model.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from brain.config import load_config
from brain.core import Core

DIM = 512  # enough buckets that unrelated words rarely collide


class FakeEmbedder:
    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        for word in text.lower().split():
            word = word.strip(".,!?:;()[]\"'")
            if not word:
                continue
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM
            v[bucket] += 1.0
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else v

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


def write_config(tmp_path: Path, collections: list[dict], calendar_toml: str = "") -> Path:
    lines = [
        "[settings]",
        'data_dir = "data"',
        "similarity_floor = 0.3",
        "context_token_budget = 8000",
        'default_model = "claude-sonnet-4-6"',
        'models = ["claude-sonnet-4-6", "claude-fable-5"]',
        "",
    ]
    for c in collections:
        root = tmp_path / "docs" / c["name"]
        root.mkdir(parents=True, exist_ok=True)
        lines += [
            "[[collection]]",
            f'name = "{c["name"]}"',
            f'roots = ["{root.as_posix()}"]',
            f'assist_level = "{c["assist_level"]}"',
            f'color = "{c.get("color", "#336699")}"',
            "",
        ]
    cfg = tmp_path / "config.toml"
    cfg.write_text("\n".join(lines) + calendar_toml, encoding="utf-8")
    return cfg


def make_core(tmp_path: Path, collections: list[dict], calendar_toml: str = "") -> Core:
    cfg_path = write_config(tmp_path, collections, calendar_toml)
    return Core(load_config(cfg_path), embedder=FakeEmbedder())


def add_doc(tmp_path: Path, collection: str, filename: str, text: str) -> Path:
    p = tmp_path / "docs" / collection / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
