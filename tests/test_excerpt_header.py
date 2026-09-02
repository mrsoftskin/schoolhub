"""What the per-excerpt header sends to the model.

Every excerpt carried the full absolute source path. The model never needed
it: answers cite by bracketed number, and the human-visible citation chip is
built by the frontend from the Citation object, which still carries the full
path. Meanwhile the directory prefix is identical for every excerpt from a
collection, Windows paths tokenize badly, and none of it is cached, so it was
paid again per excerpt on every turn.

Measured over the live index (43,342 chunks): 39.9 -> 27.8 tokens of header
per excerpt, mean. Two path components rather than one because a bare
basename made 34 files indistinguishable from another file in the SAME
collection.
"""

from __future__ import annotations

from brain.ask import _render_context, _short_source


class _Hit:
    def __init__(self, collection, source_path, locator, text, score=0.9):
        self.collection = collection
        self.source_path = source_path
        self.locator = locator
        self.text = text
        self.score = score


WIN = r"C:\Users\student\Downloads\School\Fall2026\FINC313\readings\Treasury yields.pdf"


def test_keeps_the_parent_directory_and_the_filename():
    assert _short_source(WIN) == "readings/Treasury yields.pdf"


def test_posix_paths_too():
    assert _short_source("/home/x/Projects/mavis/_Backlog.md") == "mavis/_Backlog.md"


def test_files_that_share_a_basename_stay_distinguishable():
    """The reason this is not a bare basename. Both of these are _Backlog.md
    in the same collection; under basename alone the two excerpts would differ
    in no visible way."""
    a = _short_source(r"C:/v/Projects/MavisCallGrader/_Backlog.md")
    b = _short_source(r"C:/v/Projects/mavis/_Backlog.md")
    assert a != b
    assert a.endswith("_Backlog.md") and b.endswith("_Backlog.md")


def test_short_and_odd_paths_do_not_crash():
    assert _short_source("file.md") == "file.md"
    assert _short_source("") == ""
    assert _short_source("a/b/") == "a/b"


def test_trailing_separators_are_ignored():
    assert _short_source(r"C:\x\y\z\\") == "y/z"


def test_the_rendered_header_no_longer_carries_the_absolute_path():
    hits = [_Hit("FINC313", WIN, "p. 4", "Duration measures rate sensitivity.")]
    out = _render_context(hits)
    assert "C:\\Users\\student" not in out
    assert "Downloads" not in out
    # Still fully attributable: number, collection, file, locator.
    assert "[1]" in out
    assert "collection: FINC313" in out
    assert "readings/Treasury yields.pdf" in out
    assert "p. 4" in out
    assert "Duration measures rate sensitivity." in out


def test_the_header_actually_got_smaller():
    hits = [_Hit("FINC313", WIN, "p. 4", "x")]
    header = _render_context(hits).splitlines()[2]
    assert len(header) < len(WIN)
