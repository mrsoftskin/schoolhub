"""Clearing announcements from the app.

check_news(apply=True) is the only thing that marks an announcement seen and
files it under <course>/_synced/announcements/ so Chat can cite it. It was
reachable from exactly one place, `brain sync news --apply` on the CLI: the
poller calls apply=False, and /api/sync/apply is the calendar path and never
touches news. So from the app there was no way to clear an announcement, and
because the count feeds syncTotals() a single unread one also permanently
suppressed the "No new deadlines" line.
"""

from __future__ import annotations

import time
import types

from fastapi.testclient import TestClient

from brain import sync as syncmod
from brain.sync_daemon import SyncStatus
from brain.web import app as webapp
from conftest import make_core


class _Poller:
    def __init__(self):
        self.status = SyncStatus()
        self.polls = 0

    def poll_once(self):
        self.polls += 1
        return self.status


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "FINC313", "assist_level": "full"}])
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    client = TestClient(webapp.create_app(), headers={"host": "127.0.0.1"})
    client.app.state.sync_poller = _Poller()
    return client, core


def _report(new=(), saved=0, errors=()):
    return types.SimpleNamespace(new=list(new), total=len(new), saved=saved,
                                 errors=list(errors))


def test_applying_marks_them_seen(tmp_path, monkeypatch):
    seen = {}

    def fake(config, *, apply=False):
        seen["apply"] = apply
        return _report(new=[{"course": "FINC313", "title": "Quiz moved"}], saved=1)

    monkeypatch.setattr(syncmod, "check_news", fake)
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/sync/news/apply")
    assert r.status_code == 200
    # The whole point: the dry-run path can never clear anything.
    assert seen["apply"] is True
    assert r.json()["saved"] == 1
    assert r.json()["courses"] == ["FINC313"]


def test_it_does_not_run_the_calendar_apply(tmp_path, monkeypatch):
    """Folding this into /api/sync/apply would mean a user who wanted a
    deadline written also silently lost their unread announcements."""
    called = {"run": 0}
    monkeypatch.setattr(syncmod, "run",
                        lambda *a, **k: called.__setitem__("run", called["run"] + 1))
    monkeypatch.setattr(syncmod, "check_news",
                        lambda c, *, apply=False: _report(saved=0))
    client, _ = _client(tmp_path, monkeypatch)
    assert client.post("/api/sync/news/apply").status_code == 200
    assert called["run"] == 0


def test_indexing_runs_in_the_background_not_in_the_request(tmp_path, monkeypatch):
    """Embedding a course takes long enough to time out a browser, and the
    announcements are already filed by this point."""
    monkeypatch.setattr(
        syncmod, "check_news",
        lambda c, *, apply=False: _report(
            new=[{"course": "FINC313", "title": "a"}], saved=1))
    client, core = _client(tmp_path, monkeypatch)
    got = {}
    monkeypatch.setattr(core, "index",
                        lambda **kw: got.update(kw) or types.SimpleNamespace(collections=[]))

    r = client.post("/api/sync/news/apply")
    assert r.json()["indexing"] is True
    deadline = time.time() + 5
    while time.time() < deadline and "only" not in got:
        time.sleep(0.02)
    # Indexes exactly the touched courses, as a list.
    assert got.get("only") == ["FINC313"]


def test_no_courses_means_no_index_run(tmp_path, monkeypatch):
    monkeypatch.setattr(syncmod, "check_news",
                        lambda c, *, apply=False: _report(saved=0))
    client, core = _client(tmp_path, monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(core, "index",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    r = client.post("/api/sync/news/apply")
    assert r.json()["indexing"] is False
    assert called["n"] == 0


def test_site_errors_are_surfaced_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        syncmod, "check_news",
        lambda c, *, apply=False: _report(errors=[("oaks", "session expired")]))
    client, _ = _client(tmp_path, monkeypatch)
    body = client.post("/api/sync/news/apply").json()
    assert body["errors"] == [{"site": "oaks", "message": "session expired"}]


def test_apply_no_longer_scrapes_twice_inside_the_request(tmp_path, monkeypatch):
    """syncmod.run(apply=True) already scraped every site; a second
    poll_once() in the handler made applying cost two full network passes."""
    monkeypatch.setattr(
        syncmod, "run",
        lambda *a, **k: types.SimpleNamespace(applied=1, sites=[]))
    client, core = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "calendar_import", lambda *a, **k: None)

    r = client.post("/api/sync/apply")
    assert r.status_code == 200
    assert r.json()["applied"] == 1
    # The refresh happens, but off the request: the handler returned without
    # waiting for it, so at most one poll can have been kicked off.
    assert client.app.state.sync_poller.polls <= 1
