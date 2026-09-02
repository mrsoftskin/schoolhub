"""Grade questions must carry the syllabus WEIGHTS, not just the gradebook.

Live failure that motivated this: asked to project a FINC380 grade, the app
answered "the percentage weights are missing... I can't compute a projected
course grade". The weights were indexed the whole time - the "Grading Policy"
chunk ranks #1 (0.723) for "grading policy weights" and does not appear at all
for "what do I need on the final". The student's phrasing simply does not
retrieve a block of percentages, so it is fetched by its own vocabulary.
"""

from __future__ import annotations

import numpy as np

from brain.ask import _grading_policy_block
from brain.retrieval import Hit


class FakeRetriever:
    def __init__(self, hits_by_collection, config=None):
        self._hits = hits_by_collection
        self.config = config
        self.queries: list[tuple[str, str]] = []

    def search_collection(self, query, collection, k, *, floor=None):
        self.queries.append((query, collection))
        return self._hits.get(collection, [])


def _hit(collection, text, locator="Syllabus > Grading Policy", score=0.72):
    return Hit(chunk_id=1, collection=collection, source_path="syl.docx",
               locator=locator, text=text, score=score)


REAL = ("Grading Policy\nAssignments (4) 25%\nQuizzes (7) 15%\n"
        "Exams (2) 30%\nFinal Project 20%\nClass Participation 10%")


def test_weights_are_injected_as_authoritative_context():
    r = FakeRetriever({"FINC380": [_hit("FINC380", REAL)]})
    block = _grading_policy_block(r, "FINC380")
    assert "25%" in block and "Final Project" in block
    assert "authoritative" in block.lower()
    # It must NOT be presented as a numbered citation - the question did not
    # retrieve it, so there is nothing for the reader to look up.
    assert "[1]" not in block


def test_query_uses_the_documents_vocabulary_not_the_students():
    r = FakeRetriever({"FINC380": [_hit("FINC380", REAL)]})
    _grading_policy_block(r, "FINC380")
    query = r.queries[0][0]
    for word in ("grading", "weight", "percentage", "participation"):
        assert word in query.lower()


def test_a_grading_heading_without_numbers_is_rejected():
    """A 'Grading' heading with no percentages cannot answer the question, and
    injecting it as authoritative would invite the model to invent weights."""
    r = FakeRetriever({"FINC380": [
        _hit("FINC380", "Grading\nSee the course website for details.")]})
    assert _grading_policy_block(r, "FINC380") == ""


def test_prose_with_a_single_percentage_is_rejected():
    """Course readings are full of stray percentages (cap rates, vacancy)."""
    r = FakeRetriever({"FINC380": [
        _hit("FINC380", "The property achieved a 7% cap rate last year.")]})
    assert _grading_policy_block(r, "FINC380") == ""


def test_no_hits_is_silent_not_an_error():
    assert _grading_policy_block(FakeRetriever({}), "FINC313") == ""


def test_retrieval_failure_never_breaks_the_answer():
    class Broken(FakeRetriever):
        def search_collection(self, *a, **k):
            raise RuntimeError("index unavailable")

    assert _grading_policy_block(Broken({}), "FINC313") == ""


def test_global_mode_gathers_weights_from_every_course():
    class Cfg:
        def collection_names(self):
            return ["FINC380", "FINC313"]

    r = FakeRetriever({
        "FINC380": [_hit("FINC380", REAL)],
        "FINC313": [_hit("FINC313", "Grading\nQuizzes 40%\nFinal 60%")],
    }, config=Cfg())
    block = _grading_policy_block(r, "all")
    assert "[FINC380]" in block and "[FINC313]" in block
