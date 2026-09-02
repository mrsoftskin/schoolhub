"""Index runs must not overlap, whoever starts them.

Indexing rewrites the shared embedding store (embeddings.npy plus its id/hash
manifest), so two runs interleaving can commit a vector against text it was
not built from. The guard used to live in the web layer, which only covered
the routes it owned: SyncPoller._pull_new_content() called Core.index()
directly and bypassed it, and that path is reachable from the daemon loop and
from every /api/sync/* route. The lock now lives in Core.index() itself, so no
caller can miss it.

Also covers the index-status contract the frontend polls: a frozen elapsed
time once a run ends, and a brief mode that omits the 400-line progress log.
"""

from __future__ import annotations

import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from brain import core as coremod
from brain.errors import IndexBusy
from brain.sync_daemon import SyncPoller, SyncStatus, _report_to_status
from brain.web import app as webapp
from conftest import make_core


# ---- the lock itself -------------------------------------------------

def test_a_second_run_waits_rather_than_interleaving(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])
    order, inside = [], threading.Event()
    release = threading.Event()

    def slow(*a, **kw):
        order.append("enter")
        inside.set()
        release.wait(5)
        order.append("exit")
        return types.SimpleNamespace(collections=[])

    monkeypatch.setattr(coremod, "index_collections", slow)
    t = threading.Thread(target=lambda: core.index(), daemon=True)
    t.start()
    assert inside.wait(5)

    second = threading.Thread(target=lambda: core.index(), daemon=True)
    second.start()
    time.sleep(0.2)
    # The second run has NOT entered while the first holds the lock.
    assert order == ["enter"]
    release.set()
    t.join(5); second.join(5)
    # Strictly sequential: no enter/enter before an exit.
    assert order == ["enter", "exit", "enter", "exit"]


def test_wait_false_refuses_instead_of_queueing(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])
    inside, release = threading.Event(), threading.Event()

    def slow(*a, **kw):
        inside.set()
        release.wait(5)
        return types.SimpleNamespace(collections=[])

    monkeypatch.setattr(coremod, "index_collections", slow)
    t = threading.Thread(target=lambda: core.index(), daemon=True)
    t.start()
    assert inside.wait(5)
    with pytest.raises(IndexBusy):
        core.index(wait=False)
    release.set(); t.join(5)


def test_the_lock_is_released_when_indexing_raises(tmp_path, monkeypatch):
    """A failed run that kept the lock would wedge every later run."""
    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])

    def boom(*a, **kw):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(coremod, "index_collections", boom)
    with pytest.raises(RuntimeError):
        core.index()
    assert coremod.index_busy() is False


def test_the_background_poller_defers_instead_of_racing(tmp_path, monkeypatch):
    """This is the path that bypassed the old guard entirely: the poller
    called Core.index() directly, so a UI reindex and a 6h poll could write
    the embedding store at the same time."""
    import brain.sync_daemon as sd

    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])
    poller = SyncPoller.__new__(SyncPoller)
    poller.core = core
    poller._last_content_pull = -10_000.0        # past the throttle

    seen = {}

    def fake_index(*, only=None, force=False, progress=None, wait=True):
        seen["wait"] = wait
        seen["only"] = only
        raise IndexBusy("an index run is already in progress")

    monkeypatch.setattr(core, "index", fake_index)
    monkeypatch.setattr(sd.syncmod, "pull_files",
                        lambda *a, **k: types.SimpleNamespace(
                            files=[types.SimpleNamespace(
                                course="a", status="downloaded", bytes=42)]))
    monkeypatch.setattr(sd.syncmod, "pull_quiz_content",
                        lambda *a, **k: types.SimpleNamespace(quizzes=[]))
    monkeypatch.setattr(sd.syncmod, "pull_links", lambda *a, **k: None)

    poller._pull_new_content()          # must not raise

    # It asked, it asked non-blockingly, and it gave up rather than queueing
    # a second indexer behind the one already running.
    assert seen["only"] == ["a"]
    assert seen["wait"] is False


# ---- the status contract the frontend polls --------------------------

def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    return TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}), core


def test_elapsed_freezes_when_the_run_ends(tmp_path, monkeypatch):
    """It used to keep counting forever, so a finished payload was
    indistinguishable from one two seconds old."""
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/index/start", json={})
    deadline = time.time() + 10
    while time.time() < deadline:
        st = client.get("/api/index/status").json()
        if st["done"] and not st["running"]:
            break
        time.sleep(0.02)
    assert st["finished_at"] is not None
    first = st["elapsed_sec"]
    time.sleep(0.35)
    assert client.get("/api/index/status").json()["elapsed_sec"] == first


def test_brief_omits_the_progress_lines(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/index/start", json={})
    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get("/api/index/status").json()["done"]:
            break
        time.sleep(0.02)
    full = client.get("/api/index/status").json()
    brief = client.get("/api/index/status?brief=1").json()
    assert "lines" in full
    assert "lines" not in brief
    # The one field a poller actually renders survives.
    assert brief["message"] == full["message"]
    assert brief["running"] == full["running"]


# ---- the sync watch contract -----------------------------------------

def test_run_id_ticks_on_a_successful_poll():
    prev = SyncStatus(run_id=7)
    rep = types.SimpleNamespace(sites=[])
    assert _report_to_status(rep, prev).run_id == 8


def test_run_id_ticks_on_a_FAILED_poll_too(tmp_path, monkeypatch):
    """Drives the real exception path. A watcher waiting for its run to end
    must not hang on exactly the runs it most needs to hear about, so a poll
    that throws still has to tick the counter."""
    core = make_core(tmp_path, [{"name": "a", "assist_level": "full"}])
    poller = SyncPoller.__new__(SyncPoller)
    poller.core = core
    poller._lock = threading.Lock()
    poller._poll_lock = threading.Lock()
    poller.status = SyncStatus(run_id=3)
    poller._pull_new_content = lambda: None
    poller._notify_new = lambda s: None
    poller._save = lambda: None

    def boom(*a, **kw):
        raise RuntimeError("the campus server fell over")

    monkeypatch.setattr("brain.sync_daemon.syncmod.run", boom)

    out = poller.poll_once()
    assert out.ok is False
    assert "the campus server fell over" in out.error
    assert out.run_id == 4, "a failed poll did not tick run_id"
    assert poller.status.run_id == 4


def test_run_id_is_exposed_and_survives_a_restart(tmp_path):
    assert SyncStatus(run_id=12).to_dict()["run_id"] == 12
    import json

    from brain.sync_daemon import _state_path

    d = tmp_path / "data"
    d.mkdir()
    _state_path(d).write_text(json.dumps({"run_id": 41, "sites": []}),
                              encoding="utf-8")
    poller = SyncPoller.__new__(SyncPoller)
    poller.core = types.SimpleNamespace(
        config=types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=d)))
    assert poller._load_cached().run_id == 41
