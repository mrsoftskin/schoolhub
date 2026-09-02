"""Regression tests for the 2026-08-26 grades-review fixes: bonus math,
weighted pairing, notify injection safety, plan all-day handling, and the
cross-origin guard on state-changing endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import add_doc, make_core
from brain import grades as grades_mod
from brain import notify
from brain.connectors.sites import OaksConnector
import brain.web.app as webapp


def _item(name, score, out_of, *, graded=True, bonus=False, excluded=False,
          w_num=None, w_den=None):
    return {"name": name, "graded": graded, "score": score, "out_of": out_of,
            "bonus": bonus, "excluded": excluded,
            "weighted_num": w_num, "weighted_den": w_den}


def test_bonus_adds_to_numerator_only():
    # D2L semantics: earned bonus raises the score, never the denominator.
    s = grades_mod.summarize_course({"course": "X", "ou": 1, "items": [
        _item("Quiz", 8.0, 10.0),
        _item("Extra credit", 2.0, 2.0, bonus=True),
    ]})["summary"]
    assert s["points_earned"] == 10.0
    assert s["points_possible"] == 10.0          # bonus not in the base
    assert s["current_pct"] == 100.0
    assert s["graded_count"] == 2                # matches what OAKS shows
    assert s["bonus_count"] == 1


def test_weighted_num_den_must_pair():
    # An item carrying only one half of the weighted pair must not skew the
    # ratio: here the unpaired 30-point numerator is ignored, leaving 16/20.
    s = grades_mod.summarize_course({"course": "X", "ou": 1, "items": [
        _item("Quiz", 8.0, 10.0, w_num=16.0, w_den=20.0),
        _item("Odd", 3.0, 10.0, w_num=30.0, w_den=None),
    ]})["summary"]
    assert s["basis"] == "weighted"
    assert s["current_pct"] == 80.0


def test_refresh_failure_keeps_last_good_cache(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    good = {"fetched_at": 123.0, "courses": [{"course": "X", "items": [],
            "summary": {}}], "errors": []}
    grades_mod._write_atomic(grades_mod.cache_path(core.config), good)
    # A refresh where every site errors must keep the previous courses.
    monkeypatch.setattr(grades_mod, "REGISTRY", {"oaks": None})

    class FakeStore:
        def __init__(self, *a, **k): pass
        def has(self, name): return True
        def load(self, name): return {}

    class FakeConn:
        def list_grades(self, session, courses):
            raise RuntimeError("boom")

    monkeypatch.setattr(grades_mod, "SessionStore", FakeStore)
    monkeypatch.setattr(grades_mod, "get", lambda name: FakeConn())
    data = grades_mod.refresh(core.config)
    assert data["stale"] is True
    assert data["fetched_at"] == 123.0           # last GOOD pull preserved
    assert data["courses"] == good["courses"]
    assert data["errors"] and "boom" in data["errors"][0][1]
    assert data["checked_at"] is not None


def test_parse_grades_skips_aggregate_objects():
    objects = [
        {"Id": 1, "Name": "Quiz 1", "MaxPoints": 10.0},
        {"Id": 2, "Name": "Quizzes (category)", "GradeObjectType": 5},
        {"Id": 3, "Name": "Final Calculated Grade", "GradeObjectType": 7},
    ]
    c = OaksConnector().parse_grades(objects, [], "X", 1)
    assert [i["name"] for i in c["items"]] == ["Quiz 1"]


def test_toast_script_is_constant():
    # The injection fix: nothing user- or instructor-controlled may be
    # formatted into PowerShell source. The script is a literal constant and
    # the toast text travels as an escaped XML file.
    assert "{title}" not in notify._SCRIPT and "{body}" not in notify._SCRIPT
    assert "$args[0]" in notify._SCRIPT
    hostile = 'HW $(Invoke-WebRequest evil) `; "@'
    xml = notify._XML_TEMPLATE.format(title=notify._xml_escape(hostile),
                                      body=notify._xml_escape(hostile))
    # Escaped XML still contains the $ text as DATA; the script never
    # interpolates it, so there is nothing more to assert about execution -
    # but the quotes must be neutralized for the XML parse.
    assert '"@' not in xml.replace("&quot;@", "")


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    add_doc(tmp_path, "open", "d.md", "hi")
    core.index()
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    return TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}), core


def test_cross_origin_post_refused(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/sync/run", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    # Same-origin and header-less (CLI/curl) callers still pass the guard.
    r2 = client.post("/api/sync/run", headers={"origin": "http://127.0.0.1:8177"})
    assert r2.status_code != 403
    r3 = client.post("/api/sync/run")
    assert r3.status_code != 403
    # Extension origins are exempt (session push keeps its header gate).
    r4 = client.post("/api/session/push",
                     headers={"origin": "chrome-extension://abcdef",
                              "X-CC-Extension": "1"},
                     json={"site": "oaks", "cookies": {"a": "b"}})
    assert r4.status_code == 200


def test_grades_get_never_refreshes(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(grades_mod, "refresh",
                        lambda cfg: called.__setitem__("n", called["n"] + 1) or {})
    r = client.get("/api/grades")
    assert r.status_code == 200
    assert called["n"] == 0                      # cold cache: no network
    assert r.json().get("needs_refresh") is True
    client.post("/api/grades/refresh")
    assert called["n"] == 1                      # the explicit POST fetches


def test_plan_all_day_not_past_on_due_day(tmp_path, monkeypatch):
    from datetime import datetime
    client, core = _client(tmp_path, monkeypatch)
    conn = core.open_db()
    today = datetime.now().date().isoformat()
    try:
        conn.execute(
            "INSERT INTO events (id, course, title, starts_at, ends_at, all_day, kind, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "open", "All-day HW", f"{today}T00:00:00", None, 1,
             "quiz", "csv"),
        )
        conn.commit()
    finally:
        conn.close()
    items = client.get("/api/plan").json()["items"]
    row = next(i for i in items if i["title"] == "All-day HW")
    assert row["all_day"] is True
    assert row["past"] is False                  # due ALL day, not past at 00:01


# ---- notifications work on macOS too (most students are on Macs) --------

def test_toast_no_ops_on_unsupported_platform(monkeypatch):
    """On a platform with no notifier, toast() reports success: nothing was
    attempted, so nothing failed. Otherwise the sync status would carry a
    permanent 'notification failed' line."""
    monkeypatch.setattr(notify.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda *a, **k: called.append(a) or None)
    assert notify.toast("t", "b") is True
    assert not called                      # never shelled out


def test_toast_uses_osascript_on_macos(monkeypatch):
    import types as _t

    monkeypatch.setattr(notify.sys, "platform", "darwin")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _t.SimpleNamespace(returncode=0)

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    assert notify.toast("Command Center", "FINC313: Quiz 2") is True
    assert seen["cmd"][0] == "osascript"
    # The untrusted text is passed as ARGUMENTS, never spliced into the script.
    assert seen["cmd"][-2:] == ["Command Center", "FINC313: Quiz 2"]


def test_macos_script_is_constant_and_takes_argv():
    """Same injection guarantee as the Windows path: a hostile instructor
    title cannot close the string and run AppleScript."""
    assert "{title}" not in notify._OSA_SCRIPT
    assert "{body}" not in notify._OSA_SCRIPT
    assert "on run argv" in notify._OSA_SCRIPT
    assert "item 1 of argv" in notify._OSA_SCRIPT


def test_macos_toast_failure_is_reported(monkeypatch):
    import types as _t

    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda *a, **k: _t.SimpleNamespace(returncode=1))
    assert notify.toast("t", "b") is False


def test_macos_toast_never_raises(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("osascript missing")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    assert notify.toast("t", "b") is False
