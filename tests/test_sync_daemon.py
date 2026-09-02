"""Background sync poller: status shaping, persistence, no-raise contract."""

from __future__ import annotations

import json
import types

from brain.sync_daemon import SyncPoller, SyncStatus, _report_to_status
from brain.connectors.base import PulledItem
from brain.connectors.detect import Change, Reconciliation


class _FakeSite:
    def __init__(self, site, label, ok, recon=None, error="", age=1.0,
                 configured=True):
        self.site, self.label, self.ok = site, label, ok
        self.recon, self.error, self.session_age_h = recon, error, age
        self.configured = configured


class _FakeReport:
    def __init__(self, sites):
        self.sites = sites


def _recon(new=0, moved=0):
    r = Reconciliation()
    for i in range(new):
        r.new.append(Change(item=PulledItem(course="FINC315", title=f"Quiz {i}",
                                             date="2026-09-01", site="oaks"), kind="new"))
    for i in range(moved):
        r.moved.append(Change(item=PulledItem(course="FINC315", title=f"Exam {i}",
                                              date="2026-10-01", site="oaks"),
                              kind="moved", old_date="2026-09-30"))
    return r


def test_report_to_status_counts_and_items():
    rep = _FakeReport([
        _FakeSite("oaks", "OAKS", True, _recon(new=2, moved=1)),
        _FakeSite("connect", "Connect", False, error="no saved session\nline2"),
    ])
    st = _report_to_status(rep, SyncStatus())
    d = st.to_dict()
    assert d["total_new"] == 2 and d["total_moved"] == 1
    assert d["ok"] is True
    oaks = next(s for s in d["sites"] if s["site"] == "oaks")
    assert oaks["new"] == 2 and oaks["moved"] == 1
    # Every site row carries the same keys, healthy or not.
    assert oaks["configured"] is True
    connect = next(s for s in d["sites"] if s["site"] == "connect")
    assert connect["ok"] is False and connect["error"] == "no saved session"
    assert len(d["new_items"]) == 3   # 2 new + 1 moved
    assert any(i["kind"] == "moved" and i["old_date"] == "2026-09-30" for i in d["new_items"])


def test_never_connected_is_not_reported_as_broken():
    """The sync runs every connector in REGISTRY, so a student who only uses
    OAKS has three sites they never set up. Reporting those as failures put
    three permanent orange warnings on screen naming services they have never
    heard of, none of which they could act on."""
    rep = _FakeReport([
        _FakeSite("oaks", "OAKS", True, _recon(new=1)),
        _FakeSite("vhl", "VHL", False, error="not connected (run: brain sync login vhl)",
                  configured=False),
        _FakeSite("connect", "Connect", False, error="401 Invalid token"),
    ])
    d = _report_to_status(rep, SyncStatus()).to_dict()
    vhl = next(s for s in d["sites"] if s["site"] == "vhl")
    connect = next(s for s in d["sites"] if s["site"] == "connect")
    # Both are ok=False, but only one is a fault the student can repair.
    assert vhl["configured"] is False
    assert connect["configured"] is True
    assert d["ok"] is True          # a working OAKS still carries the sync


def test_configured_defaults_true_for_older_shapes():
    """_report_to_status reads the flag defensively, so a SiteResult from a
    build that predates it is treated as connected rather than silently
    hidden from the health line."""
    class _Old:
        site, label, ok, recon, error, session_age_h = "oaks", "OAKS", False, None, "boom", 1.0

    d = _report_to_status(_FakeReport([_Old()]), SyncStatus()).to_dict()
    assert d["sites"][0]["configured"] is True


def _fake_core(tmp_path):
    settings = types.SimpleNamespace(data_dir=tmp_path, sync_poll_minutes=0)
    config = types.SimpleNamespace(settings=settings)
    return types.SimpleNamespace(config=config, open_db=lambda: None)


def test_poll_once_never_raises_and_persists(tmp_path, monkeypatch):
    core = _fake_core(tmp_path)
    p = SyncPoller(core, interval_minutes=0)

    import brain.sync_daemon as mod
    monkeypatch.setattr(mod.syncmod, "run",
                        lambda cfg, conn, apply=False: _FakeReport([
                            _FakeSite("oaks", "OAKS", True, _recon(new=1))]))
    st = p.poll_once()
    assert st.ok and st.to_dict()["total_new"] == 1
    saved = json.loads((tmp_path / "sync_state.json").read_text(encoding="utf-8"))
    assert saved["total_new"] == 1


def test_poll_once_captures_exception(tmp_path, monkeypatch):
    core = _fake_core(tmp_path)
    p = SyncPoller(core, interval_minutes=0)
    import brain.sync_daemon as mod

    def boom(cfg, conn, apply=False):
        raise RuntimeError("network down")
    monkeypatch.setattr(mod.syncmod, "run", boom)
    st = p.poll_once()
    assert st.ok is False and "network down" in st.error


def test_cached_status_loaded_on_init(tmp_path):
    (tmp_path / "sync_state.json").write_text(json.dumps({
        "last_run": 123.0, "ok": True, "sites": [{"site": "oaks", "new": 4, "moved": 0}],
        "new_items": [], "error": "",
    }), encoding="utf-8")
    core = _fake_core(tmp_path)
    p = SyncPoller(core, interval_minutes=0)
    assert p.status.last_run == 123.0
    assert p.status.to_dict()["total_new"] == 4


def test_disabled_interval_never_starts_thread(tmp_path):
    p = SyncPoller(_fake_core(tmp_path), interval_minutes=0)
    p.start()
    assert p._thread is None


# ---- honest ok flag ------------------------------------------------------

def test_all_sites_failed_is_not_ok():
    """A poll where every site failed must not report ok - a completely dead
    sync (all sessions expired) used to look healthy on the dashboard."""
    rep = _FakeReport([
        _FakeSite("oaks", "OAKS", False, error="session expired"),
        _FakeSite("vhl", "VHL", False, error="login required"),
    ])
    st = _report_to_status(rep, SyncStatus())
    assert st.ok is False
    assert "2 site(s) failed" in st.error


def test_partial_failure_stays_ok():
    """One dead site among working ones is visible per-site; the poll itself
    is still ok, so the dashboard should not scream."""
    rep = _FakeReport([
        _FakeSite("oaks", "OAKS", True, recon=_recon(new=1)),
        _FakeSite("connect", "Connect", False, error="token refresh failed"),
    ])
    st = _report_to_status(rep, SyncStatus())
    assert st.ok is True
    assert st.error == ""


def test_no_sites_is_ok():
    st = _report_to_status(_FakeReport([]), SyncStatus())
    assert st.ok is True


# ---- notified.json stays bounded ----------------------------------------

def _poller(tmp_path):
    settings = types.SimpleNamespace(data_dir=tmp_path, sync_poll_minutes=0)
    config = types.SimpleNamespace(settings=settings)
    core = types.SimpleNamespace(config=config, open_db=lambda: None)
    return SyncPoller(core, 0)


def test_notified_prunes_old_keys(tmp_path):
    from datetime import date, timedelta
    from brain.sync_daemon import NOTIFIED_KEEP_DAYS

    old = (date.today() - timedelta(days=NOTIFIED_KEEP_DAYS + 5)).isoformat()
    recent = date.today().isoformat()
    kept = _poller(tmp_path)._prune_notified({"a|old": old, "a|new": recent})
    assert "a|old" not in kept and "a|new" in kept


def test_notified_hard_caps(tmp_path):
    from datetime import date
    from brain.sync_daemon import NOTIFIED_MAX_KEYS

    today = date.today().isoformat()
    seen = {f"a|{i}": today for i in range(NOTIFIED_MAX_KEYS + 50)}
    assert len(_poller(tmp_path)._prune_notified(seen)) == NOTIFIED_MAX_KEYS


def test_notified_reads_legacy_list_without_retoasting(tmp_path):
    """Upgrading from the old plain-list format must not re-announce the
    backlog: legacy keys count as already seen."""
    (tmp_path / "notified.json").write_text(json.dumps(["a|1", "i|oaks|X|Y|Z"]),
                                            encoding="utf-8")
    seen = _poller(tmp_path)._load_notified()
    assert "a|1" in seen and "i|oaks|X|Y|Z" in seen


def test_notified_survives_corrupt_file(tmp_path):
    (tmp_path / "notified.json").write_text("{not json", encoding="utf-8")
    assert _poller(tmp_path)._load_notified() == {}
