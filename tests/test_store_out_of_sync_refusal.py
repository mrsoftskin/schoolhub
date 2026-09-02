"""An interrupted index must not dead-end the user.

Chunks are committed to SQLite and vectors are saved on a checkpoint, so a run
that is killed part-way leaves chunks with no embedding. Retrieval then fails
loudly, which is correct, but the only instruction was "run: brain index
--collection X" - a CLI command a friend has no terminal for. The app can fix
this itself now that /api/index/start exists, so the failure is reported as a
named refusal reason carrying the collection, not as prose.

Hit for real on 2026-09-01: 11,081 obsidian chunks left unembedded by a server
kill, and every Everything-scope question failed.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from brain.errors import StoreOutOfSync
from brain.web import app as webapp
from conftest import add_doc, make_core


def _events(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    out, name = [], None
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            out.append((name, json.loads(line.split(":", 1)[1].strip())))
            name = None
    return out


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "obsidian", "assist_level": "full"}])
    add_doc(tmp_path, "obsidian", "n.md", "hello")
    core.index()
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    return TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}), core


def test_the_error_carries_the_collection_to_reindex():
    e = StoreOutOfSync("boom", collection="obsidian")
    assert e.collection == "obsidian"
    assert str(e) == "boom"


def test_collection_defaults_to_none_for_callers_that_have_no_single_one():
    """embeddings.py raises this about the store as a whole."""
    assert StoreOutOfSync("whole store is bad").collection is None


def test_ask_reports_it_as_a_named_refusal_not_a_generic_error(tmp_path, monkeypatch):
    client, core = _client(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise StoreOutOfSync(
            "Collection 'obsidian': 11081 chunks have no embedding. "
            "The store is out of sync - run: brain index --collection obsidian",
            collection="obsidian")

    monkeypatch.setattr(core, "prepare_ask", boom)
    r = client.post("/api/ask", json={"question": "when is the final",
                                      "collection": "obsidian"})
    assert r.status_code == 200          # streamed, so the failure is in-band
    events = _events(r.text)
    kinds = [n for n, _ in events]
    assert "refusal" in kinds, kinds
    assert "error" not in kinds, "fell through to the generic handler"

    payload = next(d for n, d in events if n == "refusal")
    # The frontend branches on this, not on the prose.
    assert payload["reason"] == "store_out_of_sync"
    assert payload["collection"] == "obsidian"
    assert "11081" in payload["detail"]


def test_a_storewide_failure_still_refuses_cleanly(tmp_path, monkeypatch):
    """No collection to name, so the caller offers a full reindex instead."""
    client, core = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        core, "prepare_ask",
        lambda *a, **kw: (_ for _ in ()).throw(StoreOutOfSync("store is corrupt")))
    payload = next(d for n, d in _events(
        client.post("/api/ask", json={"question": "x",
                                      "collection": "obsidian"}).text)
        if n == "refusal")
    assert payload["reason"] == "store_out_of_sync"
    assert payload["collection"] is None


def test_other_refusals_are_unchanged(tmp_path, monkeypatch):
    """The existing reasons the frontend already branches on must keep working."""
    from brain.errors import NoRelevantResults

    client, core = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        core, "prepare_ask",
        lambda *a, **kw: (_ for _ in ()).throw(NoRelevantResults(0.6)))
    payload = next(d for n, d in _events(
        client.post("/api/ask", json={"question": "x",
                                      "collection": "obsidian"}).text)
        if n == "refusal")
    assert payload["reason"] == "no_results"
