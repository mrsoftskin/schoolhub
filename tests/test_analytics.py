"""Dashboard aggregates: the figures a person reads off the Today and
Analytics tabs must match the underlying events and chunks."""

from __future__ import annotations

from datetime import datetime

from brain import analytics
from brain import calendar as cal
from brain.config import load_config
from brain.db import connect
from conftest import add_doc, make_core, write_config

CAL_TOML = """
[calendar]
ics_paths = []
fixed_csv = "calendar/fixed.csv"
semester_start = 2026-09-07
semester_end = 2026-09-25

[[calendar.recurring]]
course = "TEST101"
title = "TEST 101 class"
weekdays = ["Mon", "Wed", "Fri"]
start = "09:00"
end = "09:50"

[[calendar.recurring]]
course = "QUIET202"
title = "QUIET 202 class"
weekdays = ["Tue"]
start = "10:00"
end = "11:00"
"""

CSV = """course,title,date,start_time,end_time,all_day,kind
TEST101,Quiz 1,2026-09-08,12:00,,false,quiz
TEST101,Big Exam,2026-09-21,09:00,10:30,false,exam
TEST101,Paper,2026-09-22,23:59,,false,project
TEST101,Reading day,2026-09-23,,,true,admin
"""


def _setup(tmp_path):
    csv_path = tmp_path / "calendar" / "fixed.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(CSV, encoding="utf-8")
    cfg_path = write_config(
        tmp_path,
        [{"name": "TEST101", "assist_level": "full", "color": "#3987e5"}],
        CAL_TOML,
    )
    config = load_config(cfg_path)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    return config, conn


def test_by_course_counts_only_what_is_ahead(tmp_path):
    config, conn = _setup(tmp_path)
    now = datetime(2026, 9, 9, 0, 0)  # after Quiz 1, before the rest
    rows = {c["course"]: c for c in analytics.by_course(conn, config, now)}
    t = rows["TEST101"]
    assert t["total"] == 3          # every dated item
    assert t["remaining"] == 2      # Quiz 1 has passed
    assert t["exam"] == 1 and t["project"] == 1 and t["quiz"] == 0
    assert t["next_title"] == "Big Exam"
    assert t["days_until_next"] == 12
    conn.close()


def test_course_with_no_dated_work_still_appears(tmp_path):
    """A course that meets but has published no due dates must not vanish
    from a table titled 'by course'."""
    config, conn = _setup(tmp_path)
    rows = {c["course"]: c for c in analytics.by_course(conn, config, datetime(2026, 9, 9))}
    assert "QUIET202" in rows
    q = rows["QUIET202"]
    assert q["total"] == 0 and q["remaining"] == 0
    assert q["note"] == "no dated work published"
    conn.close()


def test_semester_progress(tmp_path):
    config, conn = _setup(tmp_path)
    p = analytics.semester_progress(config, datetime(2026, 9, 16).date())
    assert p["total_days"] == 18
    assert p["elapsed_days"] == 9
    assert p["pct_elapsed"] == 50.0
    assert p["days_remaining"] == 9
    conn.close()


def test_daily_load_matches_events(tmp_path):
    config, conn = _setup(tmp_path)
    now = datetime(2026, 9, 7, 0, 0)
    daily = analytics.daily_load(conn, config, now, days=21)
    counts = {d["date"]: d["count"] for d in daily}
    assert counts["2026-09-08"] == 1     # Quiz 1
    assert counts["2026-09-21"] == 1     # Big Exam
    assert counts["2026-09-22"] == 1     # Paper
    assert counts["2026-09-23"] == 0     # admin excluded from deadlines
    assert sum(counts.values()) == 3
    assert daily[1]["courses"] == {"TEST101": 1}
    conn.close()


def test_week_load_by_course_matches_plain_week_load(tmp_path):
    """The stacked view and the heat row must never disagree."""
    config, conn = _setup(tmp_path)
    wl = analytics.week_load_by_course(conn, config)
    plain = {w["week_start"]: w["count"] for w in cal.week_load(conn, config)}
    for w in wl["weeks"]:
        assert sum(w["by_course"].values()) == plain[w["week_start"]]
        assert w["count"] == plain[w["week_start"]]
    conn.close()


def test_index_composition_and_file_types(tmp_path):
    core = make_core(tmp_path, [
        {"name": "alpha", "assist_level": "full"},
        {"name": "beta", "assist_level": "off"},
    ])
    add_doc(tmp_path, "alpha", "a.md", "zorbulon content here.")
    add_doc(tmp_path, "alpha", "b.txt", "more zorbulon content.")
    core.index()
    conn = core.open_db()
    try:
        comp = {c["collection"]: c for c in analytics.index_composition(conn, core.config)}
        assert comp["alpha"]["docs"] == 2
        assert comp["alpha"]["chunks"] >= 2
        # An empty collection is listed at zero, not omitted.
        assert comp["beta"]["chunks"] == 0
        assert comp["beta"]["assist_level"] == "off"
        assert abs(sum(c["share"] for c in comp.values()) - 100.0) < 0.2

        types = {t["ext"]: t["chunks"] for t in analytics.file_types(conn)}
        assert types["md"] >= 1 and types["txt"] >= 1
    finally:
        conn.close()


def test_build_totals_are_internally_consistent(tmp_path):
    config, conn = _setup(tmp_path)
    a = analytics.build(conn, config, now=datetime(2026, 9, 9))
    t = a["totals"]
    assert t["deadlines_remaining"] == sum(c["remaining"] for c in a["by_course"])
    assert t["exams_remaining"] == sum(c["exam"] for c in a["by_course"])
    assert t["collections_total"] == len(a["index"])
    conn.close()


def test_busiest_days_only_reports_real_pileups(tmp_path):
    config, conn = _setup(tmp_path)
    # Nothing in this fixture doubles up, so nothing should be reported.
    assert analytics.busiest_days(conn, datetime(2026, 9, 1)) == []
    conn.close()
