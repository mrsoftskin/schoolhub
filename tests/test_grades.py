"""Grade tracking: parsing, summary math, digest injection."""

from __future__ import annotations

import json
import time
import types

from brain import grades as grades_mod
from brain.connectors.sites import OaksConnector


def test_parse_grades_joins_objects_and_values():
    objects = [
        {"Id": 1, "Name": "Quiz 1", "MaxPoints": 10.0, "Weight": 20.0,
         "IsBonus": False, "ExcludeFromFinalGradeCalculation": False},
        {"Id": 2, "Name": "Hidden thing", "IsHidden": True},
        {"Id": 3, "Name": "Final", "MaxPoints": 100.0, "Weight": 40.0},
    ]
    values = [{"GradeObjectIdentifier": "1", "PointsNumerator": 8.0,
               "PointsDenominator": 10.0, "DisplayedGrade": "80 %",
               "WeightedNumerator": 16.0, "WeightedDenominator": 20.0}]
    c = OaksConnector().parse_grades(objects, values, "FINC380", 399516)
    assert c["course"] == "FINC380"
    names = [i["name"] for i in c["items"]]
    assert names == ["Quiz 1", "Final"]          # hidden dropped
    q = c["items"][0]
    assert q["graded"] and q["score"] == 8.0 and q["displayed"] == "80 %"
    assert not c["items"][1]["graded"]


def test_summarize_weighted_beats_points():
    course = {"course": "SPAN200", "ou": 1, "items": [
        {"name": "PV 1", "graded": True, "score": 9.75, "out_of": 10.0,
         "bonus": False, "excluded": False,
         "weighted_num": 9.75, "weighted_den": 10.0},
        {"name": "Final", "graded": False, "score": None, "out_of": 100.0,
         "bonus": False, "excluded": False,
         "weighted_num": None, "weighted_den": None},
    ]}
    s = grades_mod.summarize_course(course)["summary"]
    assert s["current_pct"] == 97.5 and s["basis"] == "weighted"
    assert s["graded_count"] == 1 and s["total_count"] == 2


def test_summarize_points_fallback_and_empty():
    course = {"course": "X", "ou": 1, "items": [
        {"name": "Q", "graded": True, "score": 8.0, "out_of": 10.0,
         "bonus": False, "excluded": False,
         "weighted_num": None, "weighted_den": None},
    ]}
    s = grades_mod.summarize_course(course)["summary"]
    assert s["current_pct"] == 80.0 and s["basis"] == "points"
    empty = grades_mod.summarize_course({"course": "Y", "ou": 2, "items": []})
    assert empty["summary"]["current_pct"] is None


def _cfg(tmp_path):
    settings = types.SimpleNamespace(data_dir=tmp_path)
    return types.SimpleNamespace(settings=settings)


def test_digest_reads_cache_only_and_respects_staleness(tmp_path):
    cfg = _cfg(tmp_path)
    assert grades_mod.digest(cfg) == ""            # no cache -> empty
    data = {"fetched_at": time.time(), "errors": [], "courses": [
        grades_mod.summarize_course({"course": "FINC380", "ou": 1, "items": [
            {"name": "Quiz 1", "graded": True, "score": 8.0, "out_of": 10.0,
             "bonus": False, "excluded": False, "displayed": "80 %",
             "weighted_num": None, "weighted_den": None}]})]}
    (tmp_path / "grades.json").write_text(json.dumps(data), encoding="utf-8")
    d = grades_mod.digest(cfg)
    assert "GRADES" in d and "Quiz 1 = 8/10" in d and "80.0%" in d  # trimmed scores
    # scoped to another collection -> empty
    assert grades_mod.digest(cfg, "SPAN200") == ""
    # stale cache -> empty
    data["fetched_at"] = time.time() - 999 * 3600
    (tmp_path / "grades.json").write_text(json.dumps(data), encoding="utf-8")
    assert grades_mod.digest(cfg) == ""


def test_refresh_never_clobbers_good_cache_on_total_failure(tmp_path, monkeypatch):
    # Morning-after scenario: session expired overnight, every site errors.
    # The previous night's courses must survive, marked stale.
    import brain.grades as gm
    from brain.connectors.base import LoginRequired as LR
    from brain.connectors import sites

    cfg = _cfg(tmp_path)
    cfg.collection_names = lambda: ["FINC380"]
    good = {"fetched_at": time.time() - 6 * 3600, "errors": [], "courses": [
        grades_mod.summarize_course({"course": "FINC380", "ou": 1, "items": [
            {"name": "Quiz 1", "graded": True, "score": 8.0, "out_of": 10.0,
             "bonus": False, "excluded": False, "displayed": "80 %",
             "weighted_num": None, "weighted_den": None}]})]}
    (tmp_path / "grades.json").write_text(json.dumps(good), encoding="utf-8")

    monkeypatch.setattr(gm.SessionStore, "has", lambda self, n: n == "oaks")
    monkeypatch.setattr(gm.SessionStore, "load", lambda self, n: {"cookies": {}})

    def dead(self, session, courses):
        raise LR("oaks: the saved session was rejected")
    monkeypatch.setattr(sites.OaksConnector, "list_grades", dead)

    out = gm.refresh(cfg)
    assert out["stale"] is True
    assert len(out["courses"]) == 1                  # yesterday's data kept
    assert out["errors"] and "rejected" in out["errors"][0][1]
    on_disk = json.loads((tmp_path / "grades.json").read_text(encoding="utf-8"))
    assert len(on_disk["courses"]) == 1 and on_disk["stale"] is True
