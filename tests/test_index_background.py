"""The non-blocking index route.

/api/index runs inside the request, which turns a first-run index of a real
library into a minutes-long hung connection with nothing on screen. These
tests cover the background pair that fixes that (/api/index/start plus
/api/index/status), and the serialization that stops two index runs from
sharing one embedding store.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from brain.web import app as webapp
from conftest import add_doc, make_core


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    add_doc(tmp_path, "open", "d.md", "hello world")
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    return TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}), core


def _wait_done(client, timeout=10.0):
    """Poll status until the job reports done, like the frontend would."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/index/status").json()
        if st["done"] and not st["running"]:
            return st
        time.sleep(0.02)
    pytest.fail(f"index job did not finish within {timeout}s: {st}")


def test_status_is_answerable_before_any_run(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    st = client.get("/api/index/status").json()
    assert st["running"] is False
    assert st["done"] is False
    assert st["error"] is None
    assert st["elapsed_sec"] is None
    assert st["message"] == ""


def test_start_returns_immediately_and_finishes(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/index/start", json={})
    assert r.status_code == 200
    assert r.json()["running"] is True

    st = _wait_done(client)
    assert st["error"] is None
    # Same payload shape the synchronous route returns.
    assert st["report"]["collections"][0]["collection"] == "open"
    assert st["report"]["collections"][0]["indexed"] == 1
    assert st["elapsed_sec"] is not None


def test_progress_lines_are_captured(tmp_path, monkeypatch):
    """The whole point: something to show while it runs."""
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/index/start", json={})
    st = _wait_done(client)
    assert st["lines"], "no progress was recorded"
    assert any("open" in line for line in st["lines"])
    assert st["message"]


def test_unknown_collection_is_refused_before_the_thread_starts(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/index/start", json={"collection": "nope"})
    assert r.status_code == 404
    # And the job state was not left claiming to run.
    assert client.get("/api/index/status").json()["running"] is False


def test_second_run_is_refused_while_one_is_active(tmp_path, monkeypatch):
    """Two indexers sharing one embedding store overwrite each other."""
    client, core = _client(tmp_path, monkeypatch)
    release = threading.Event()
    real_index = core.index

    def slow_index(*a, **kw):
        assert release.wait(10), "test never released the blocked index"
        return real_index(*a, **kw)

    monkeypatch.setattr(core, "index", slow_index)

    assert client.post("/api/index/start", json={}).status_code == 200
    try:
        # Both routes contend for the same flag.
        assert client.post("/api/index/start", json={}).status_code == 409
        assert client.post("/api/index", json={}).status_code == 409
    finally:
        release.set()
    _wait_done(client)


def test_worker_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    """An exception on the worker thread would otherwise vanish with the
    thread, leaving status stuck on running forever."""
    client, core = _client(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(core, "index", boom)
    client.post("/api/index/start", json={})
    st = _wait_done(client)
    assert st["running"] is False
    assert "disk went away" in st["error"]
    assert st["report"] is None


def test_sync_route_keeps_its_shape_and_releases_the_flag(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/index", json={})
    assert r.status_code == 200
    assert r.json()["collections"][0]["collection"] == "open"
    # Not left holding the lock: a second call must still be accepted.
    assert client.post("/api/index", json={}).status_code == 200
    st = client.get("/api/index/status").json()
    assert st["running"] is False
    # A poll after a synchronous run shows the finished report, not
    # "done, with no report", which would read as an index that found nothing.
    assert st["done"] is True
    assert st["report"]["collections"][0]["collection"] == "open"
