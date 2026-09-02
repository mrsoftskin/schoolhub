"""Retrieval: per-collection top-k with no pool merging, similarity floor,
and loud empty-index behavior."""

from __future__ import annotations

import pytest

from brain.errors import BrainError, EmptyIndexError
from conftest import add_doc, make_core

TWO = [
    {"name": "big", "assist_level": "full"},
    {"name": "small", "assist_level": "full"},
]


def _indexed_core(tmp_path):
    core = make_core(tmp_path, TWO)
    # 'big' has many docs about zorbulon; 'small' has exactly one.
    for i in range(10):
        add_doc(tmp_path, "big", f"b{i}.md",
                f"zorbulon flarnak details variant {i}. zorbulon flarnak notes.")
    add_doc(tmp_path, "small", "s.md", "zorbulon flarnak summary from the small side.")
    add_doc(tmp_path, "small", "other.md", "completely unrelated quimbat material here.")
    core.index()
    return core


def test_global_search_is_per_collection_topk(tmp_path):
    core = _indexed_core(tmp_path)
    conn = core.open_db()
    try:
        per = core.retriever(conn).search_global("zorbulon flarnak", 3)
    finally:
        conn.close()
    # No merged pool: 'big' cannot crowd out 'small'.
    assert set(per.keys()) == {"big", "small"}
    assert len(per["big"]) <= 3
    assert len(per["small"]) >= 1
    for name, hits in per.items():
        for h in hits:
            assert h.collection == name  # every chunk tagged with its collection


def test_similarity_floor_filters_everything(tmp_path):
    core = _indexed_core(tmp_path)
    conn = core.open_db()
    try:
        r = core.retriever(conn)
        hits = r.search_collection("xylophone breakfast astronomy", "big", 5)
        assert hits == []
        per = r.search_global("xylophone breakfast astronomy", 5)
        assert per == {}
    finally:
        conn.close()


def test_floor_boundary_keeps_scores_at_or_above(tmp_path):
    core = _indexed_core(tmp_path)
    conn = core.open_db()
    try:
        hits = core.retriever(conn).search_collection("zorbulon flarnak", "big", 10)
    finally:
        conn.close()
    assert hits, "expected relevant hits"
    for h in hits:
        assert h.score >= core.config.settings.similarity_floor


def test_empty_index_raises_loudly(tmp_path):
    core = make_core(tmp_path, TWO)  # nothing indexed at all
    conn = core.open_db()
    try:
        r = core.retriever(conn)
        with pytest.raises(EmptyIndexError):
            r.search_collection("anything", "big", 3)
        with pytest.raises(EmptyIndexError):
            r.search_global("anything", 3)
    finally:
        conn.close()


def test_one_empty_collection_is_skipped_in_global(tmp_path):
    core = make_core(tmp_path, TWO)
    add_doc(tmp_path, "big", "b.md", "zorbulon flarnak content lives here.")
    core.index()  # 'small' stays empty
    conn = core.open_db()
    try:
        r = core.retriever(conn)
        per = r.search_global("zorbulon flarnak", 3)
        assert set(per.keys()) == {"big"}
        assert r.empty_collections() == ["small"]
    finally:
        conn.close()


def test_incremental_index_skips_unchanged(tmp_path):
    core = make_core(tmp_path, TWO)
    add_doc(tmp_path, "big", "b.md", "zorbulon flarnak content.")
    r1 = core.index()
    assert r1.collections[0].indexed == 1
    r2 = core.index()
    big2 = next(c for c in r2.collections if c.collection == "big")
    assert big2.indexed == 0
    assert big2.skipped == 1
    r3 = core.index(force=True)
    big3 = next(c for c in r3.collections if c.collection == "big")
    assert big3.indexed == 1


def test_interrupted_index_does_not_leave_vectors_pointing_at_wrong_text(tmp_path):
    """SQLite reuses freed rowids. If a run dies between the DB commit and the
    store save, a reused id can end up matched with the vector of the text it
    replaced - answering confidently from the wrong source. The content hash
    must catch that, and a plain reindex must repair it."""
    core = make_core(tmp_path, TWO)
    doc = tmp_path / "docs" / "big" / "z.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# A\nalphaword alphaword alphaword.\n\n# B\nbetaword betaword.\n"
                   "\n# C\ngammaword gammaword.\n", encoding="utf-8")
    core.index()

    # Rewrite the file to produce fewer chunks, then fail during embedding -
    # exactly the crash window. The DB commit has already happened.
    doc.write_text("# Z\nzuluword zuluword.\n", encoding="utf-8")

    class Boom(Exception):
        pass

    class FailingEmbedder:
        def __init__(self, inner):
            self.inner = inner

        def embed_docs(self, texts):
            raise Boom("killed mid-embed")

        def embed_query(self, text):
            return self.inner.embed_query(text)

    good = core.embedder
    core.embedder = FailingEmbedder(good)
    with pytest.raises(Boom):
        core.index()
    core.embedder = good

    # The store now holds a vector for a rowid whose text has changed.
    conn = core.open_db()
    try:
        with pytest.raises(BrainError) as exc:
            core.retriever(conn).search_collection("alphaword alphaword", "big", 3)
        assert "no longer match" in str(exc.value) or "no embedding" in str(exc.value)
    finally:
        conn.close()

    # A plain reindex (no --force) must repair it.
    core.index()
    conn = core.open_db()
    try:
        r = core.retriever(conn)
        assert r.search_collection("zuluword zuluword", "big", 3), "current text must be findable"
        # Text that no longer exists must not score against a stale vector.
        assert r.search_collection("alphaword alphaword alphaword", "big", 3) == []
    finally:
        conn.close()


def test_zero_chunk_file_is_reported_as_a_failure(tmp_path):
    """A file we extract nothing from is a failure, not a success: recording
    it as indexed hides it forever behind the mtime/size skip."""
    core = make_core(tmp_path, TWO)
    add_doc(tmp_path, "big", "empty.md", "   \n\n   \n")
    add_doc(tmp_path, "big", "good.md", "zorbulon flarnak real content.")
    report = core.index()
    big = next(c for c in report.collections if c.collection == "big")
    assert big.indexed == 1
    assert len(big.failures) == 1
    assert "zero chunks" in big.failures[0].reason
    assert "empty.md" in big.failures[0].path


def test_unavailable_root_does_not_delete_the_collection(tmp_path):
    """An unplugged drive or a syncing cloud folder makes discovery return
    nothing; that must not be read as 'every file was deleted'."""
    import shutil

    core = make_core(tmp_path, TWO)
    add_doc(tmp_path, "big", "a.md", "zorbulon flarnak content one.")
    add_doc(tmp_path, "big", "b.md", "zorbulon flarnak content two.")
    core.index()
    conn = core.open_db()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE collection='big'").fetchone()["n"]
    finally:
        conn.close()
    assert before > 0

    root = tmp_path / "docs" / "big"
    stashed = tmp_path / "stashed"
    shutil.move(str(root), str(stashed))
    report = core.index(only=["big"])
    big = report.collections[0]
    assert big.removed == 0, "must not delete entries for an unreadable root"
    assert any("root path does not exist" in f.reason for f in big.failures)

    conn = core.open_db()
    try:
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE collection='big'").fetchone()["n"]
    finally:
        conn.close()
    assert after == before

    # And once the root is back, normal deletion resumes.
    shutil.move(str(stashed), str(root))
    (root / "b.md").unlink()
    report = core.index(only=["big"])
    assert report.collections[0].removed == 1


def test_parse_failures_reported_not_fatal(tmp_path):
    core = make_core(tmp_path, TWO)
    add_doc(tmp_path, "big", "good.md", "zorbulon flarnak fine content.")
    bad = tmp_path / "docs" / "big" / "bad.pdf"
    bad.write_bytes(b"not really a pdf")
    report = core.index()
    big = next(c for c in report.collections if c.collection == "big")
    assert big.indexed == 1
    assert len(big.failures) == 1
    assert "bad.pdf" in big.failures[0].path
    # And the good file is searchable.
    conn = core.open_db()
    try:
        hits = core.retriever(conn).search_collection("zorbulon flarnak", "big", 3)
    finally:
        conn.close()
    assert len(hits) == 1


# ---- cross-platform file matching ---------------------------------------

def test_glob_match_is_case_insensitive():
    """A 'Lecture.PDF' must index exactly like 'lecture.pdf'. On macOS/Linux
    fnmatch is case-SENSITIVE, so upper-case extensions silently fell out."""
    from brain.indexer import _glob_match

    assert _glob_match("Lecture.PDF", "**/*.pdf")
    assert _glob_match("notes/Slides.PPTX", "**/*.pptx")
    assert _glob_match("a/B/c.Md", "**/*.md")
    assert not _glob_match("archive.zip", "**/*.pdf")


def test_glob_match_still_matches_root_files():
    from brain.indexer import _glob_match

    assert _glob_match("syllabus.pdf", "**/*.pdf")      # zero directories
    assert _glob_match("wk1/syllabus.pdf", "**/*.pdf")


def test_default_excludes_match_appledouble():
    from brain.indexer import _glob_match

    assert _glob_match("._Lecture.pdf", "**/._*")
    assert _glob_match("notes/._Lecture.pdf", "**/._*")
    assert _glob_match("__MACOSX/Lecture.pdf", "**/__MACOSX/**")
