"""The non-blocking sync trigger.

/api/sync/run does the whole scrape inside the request. Measured live against
four sites it took 14.2 seconds, and connectors/http.py allows 25 seconds per
request, so a slow campus server pushes it far past that. The button that
calls it just reads "Checking..." for the duration, which is indistinguishable
from a hang. /api/sync/start returns immediately and lets the caller watch the
`running` flag the poller already maintains.
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from brain.sync_daemon import SyncStatus
from brain.web import app as webapp
from conftest import make_core


class FakePoller:
    """Stands in for SyncPoller: same surface the routes touch."""

    def __init__(self):
        self.status = SyncStatus()
        self.calls = 0
        self.release = threading.Event()
        self.entered = threading.Event()

    def poll_once(self):
        self.calls += 1
        self.status.running = True
        self.entered.set()
        self.release.wait(10)
        self.status.running = False
        self.status.last_run = time.time()
        return self.status


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    client = TestClient(webapp.create_app(), headers={"host": "127.0.0.1"})
    poller = FakePoller()
    client.app.state.sync_poller = poller
    return client, poller


def test_start_returns_before_the_scrape_finishes(tmp_path, monkeypatch):
    client, poller = _client(tmp_path, monkeypatch)
    t0 = time.time()
    r = client.post("/api/sync/start")
    elapsed = time.time() - t0
    assert r.status_code == 200
    # The fake blocks until released; returning at all proves we did not wait.
    assert elapsed < 2.0, f"start blocked for {elapsed:.1f}s"
    assert poller.entered.wait(5), "the poll never started"
    assert client.get("/api/sync/status").json()["running"] is True
    poller.release.set()


def test_status_reports_running_then_settles(tmp_path, monkeypatch):
    client, poller = _client(tmp_path, monkeypatch)
    client.post("/api/sync/start")
    assert poller.entered.wait(5)
    assert client.get("/api/sync/status").json()["running"] is True
    poller.release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get("/api/sync/status").json()["running"] is False:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("running never cleared")
    assert poller.calls == 1


def test_second_start_does_not_launch_a_competing_scrape(tmp_path, monkeypatch):
    """Two scrapes of the same sites at once is wasted network and a race on
    the poller's cached status."""
    client, poller = _client(tmp_path, monkeypatch)
    client.post("/api/sync/start")
    assert poller.entered.wait(5)
    assert client.post("/api/sync/start").status_code == 200
    poller.release.set()
    time.sleep(0.2)
    assert poller.calls == 1, f"launched {poller.calls} scrapes"


def test_run_still_blocks_and_returns_the_same_shape(tmp_path, monkeypatch):
    """The old route is unchanged, so existing callers are unaffected."""
    client, poller = _client(tmp_path, monkeypatch)
    poller.release.set()          # let it complete inline
    r = client.post("/api/sync/run")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body and "sites" in body and "total_new" in body
    assert poller.calls == 1
