"""Calendar: recurring expansion skips breaks, re-import is idempotent,
CSV parsing fails loud per row, week-load excludes admin and groups by
Monday-started weeks."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from brain import calendar as cal
from brain.config import load_config
from brain.errors import ConfigError
from conftest import write_config

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

[[calendar.breaks]]
start = 2026-09-14
end = 2026-09-16
label = "test break"
"""

CSV_GOOD = """course,title,date,start_time,end_time,all_day,kind
TEST101,Quiz 1,2026-09-08,12:00,,false,quiz
TEST101,Big Exam,2026-09-21,09:00,10:30,false,exam
TEST101,Reading day,2026-09-22,,,true,admin
"""

CSV_WITH_BAD_ROWS = CSV_GOOD + """TEST101,Bad kind row,2026-09-23,12:00,,false,homework
TEST101,Bad date row,not-a-date,12:00,,false,quiz
"""


def _config(tmp_path, csv_text=CSV_GOOD):
    csv_path = tmp_path / "calendar" / "fixed.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(csv_text, encoding="utf-8")
    cfg_path = write_config(
        tmp_path, [{"name": "TEST101", "assist_level": "full"}], CAL_TOML
    )
    return load_config(cfg_path)


def test_recurring_rule_can_produce_deadlines(tmp_path):
    """Homework due before every class is repeating graded work, not a class
    meeting: it must land in the deadline views, not be filtered out with the
    lecture blocks."""
    from brain.db import connect

    hw = CAL_TOML + """
[[calendar.recurring]]
course = "TEST101"
title = "Supersite homework (due before class)"
weekdays = ["Mon", "Wed", "Fri"]
start = "08:55"
end = "09:00"
kind = "quiz"
"""
    csv_path = tmp_path / "calendar" / "fixed.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(CSV_GOOD, encoding="utf-8")
    config = load_config(write_config(
        tmp_path, [{"name": "TEST101", "assist_level": "full"}], hw))
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)

    kinds = {r["title"]: r["kind"] for r in conn.execute("SELECT title, kind FROM events")}
    assert kinds["Supersite homework (due before class)"] == "quiz"
    assert kinds["TEST 101 class"] == "recurring"

    # It shows up as an upcoming deadline; the class meeting still does not.
    titles = [e["title"] for e in cal.next_events(conn, datetime(2026, 9, 7, 0, 0), limit=3)]
    assert "Supersite homework (due before class)" in titles
    assert "TEST 101 class" not in titles
    conn.close()


def test_recurring_rule_rejects_unknown_kind(tmp_path):
    bad = CAL_TOML + """
[[calendar.recurring]]
course = "TEST101"
title = "Homework"
weekdays = ["Mon"]
start = "08:00"
end = "08:30"
kind = "homework"
"""
    assert 'kind = "homework"' in bad
    with pytest.raises(ConfigError, match="kind"):
        load_config(write_config(
            tmp_path, [{"name": "TEST101", "assist_level": "full"}], bad))


def test_recurring_expansion_skips_breaks(tmp_path):
    config = _config(tmp_path)
    events = cal.expand_recurring(config.calendar)
    days = sorted(e.starts_at.date() for e in events)
    # MWF between Sep 7 and Sep 25, minus the Sep 14-16 break (Mon+Wed hit).
    assert date(2026, 9, 7) in days and date(2026, 9, 25) in days
    assert date(2026, 9, 14) not in days  # Monday in break
    assert date(2026, 9, 16) not in days  # Wednesday in break
    assert date(2026, 9, 18) in days      # Friday after break
    assert len(days) == 9 - 2
    ev = events[0]
    assert ev.kind == "recurring" and ev.source == "recurring"
    assert ev.starts_at.hour == 9 and ev.ends_at.hour == 9


def test_import_is_idempotent(tmp_path):
    from brain.db import connect

    config = _config(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    r1 = cal.import_calendar(config, conn)
    ids1 = {row["id"] for row in conn.execute("SELECT id FROM events")}
    r2 = cal.import_calendar(config, conn)
    ids2 = {row["id"] for row in conn.execute("SELECT id FROM events")}
    assert ids1 == ids2
    assert r1.total_imported == r2.total_imported
    n = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert n == len(ids1)  # no duplicates
    conn.close()


def test_csv_bad_rows_reported_good_rows_kept(tmp_path):
    from brain.db import connect

    config = _config(tmp_path, CSV_WITH_BAD_ROWS)
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal.import_calendar(config, conn)
    csv_report = next(s for s in report.sources if s.source == "csv")
    assert len(csv_report.errors) == 2
    assert any("homework" in e for e in csv_report.errors)
    assert any("line" in e for e in csv_report.errors)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE source = 'csv'"
    ).fetchone()["n"]
    assert n == 3  # good rows survived
    conn.close()


def test_csv_row_errors_never_delete_previously_imported_events(tmp_path):
    """An Excel resave that reformats every date must not wipe the calendar:
    a source with errors is upserted, not rebuilt."""
    from brain.db import connect

    config = _config(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='csv'").fetchone()["n"]
    assert before == 3

    excel_style = CSV_GOOD.replace("2026-09-08", "9/8/2026").replace(
        "2026-09-21", "9/21/2026").replace("2026-09-22", "9/22/2026")
    (tmp_path / "calendar" / "fixed.csv").write_text(excel_style, encoding="utf-8")
    report = cal.import_calendar(config, conn)

    csv_report = next(s for s in report.sources if s.source == "csv")
    assert len(csv_report.errors) == 3
    assert not report.full_rebuild["csv"]
    assert "csv" in report.upsert_only()
    after = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='csv'").fetchone()["n"]
    assert after == before  # nothing destroyed
    conn.close()


def test_csv_wrong_header_fails_whole_source_and_keeps_previous(tmp_path):
    from brain.db import connect

    config = _config(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='csv'").fetchone()["n"]
    assert before == 3
    (tmp_path / "calendar" / "fixed.csv").write_text("totally,wrong,header\n1,2,3\n", encoding="utf-8")
    report = cal.import_calendar(config, conn)
    csv_report = next(s for s in report.sources if s.source == "csv")
    assert csv_report.status == "failed"
    assert csv_report.errors
    after = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='csv'").fetchone()["n"]
    assert after == before  # previous events kept, loudly reported
    conn.close()


def test_one_bad_ics_does_not_delete_the_other_files_events(tmp_path):
    """The keep-previous guard must be honest: if a report says a file failed,
    that file's events must actually still be there."""
    from brain.db import connect

    def ics(path, uid, summary, day):
        path.write_text(
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
            f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{summary}\n"
            f"DTSTART:202609{day}T130000\nEND:VEVENT\nEND:VCALENDAR\n",
            encoding="utf-8")

    a, b = tmp_path / "a.ics", tmp_path / "b.ics"
    ics(a, "1@t", "TEST101 Exam From A", "21")
    ics(b, "2@t", "TEST101 Exam From B", "22")
    config = _config(tmp_path)
    config.calendar.ics_paths = [a, b]
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    titles = {r["title"] for r in conn.execute("SELECT title FROM events WHERE source='ics'")}
    assert titles == {"TEST101 Exam From A", "TEST101 Exam From B"}

    b.write_text("this is not a calendar at all", encoding="utf-8")
    report = cal.import_calendar(config, conn)
    b_report = next(s for s in report.sources if s.detail == str(b))
    assert b_report.status == "failed"
    titles_after = {r["title"] for r in conn.execute("SELECT title FROM events WHERE source='ics'")}
    assert titles_after == {"TEST101 Exam From A", "TEST101 Exam From B"}
    conn.close()


def test_week_load_excludes_admin_and_groups_by_monday(tmp_path):
    from brain.db import connect

    config = _config(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    weeks = {w["week_start"]: w["count"] for w in cal.week_load(conn, config)}
    # Week of Sep 7: MWF classes (3) + quiz = 4.
    assert weeks["2026-09-07"] == 4
    # Week of Sep 14 (break MWF -> only Fri 18th class): 1.
    assert weeks["2026-09-14"] == 1
    # Week of Sep 21: MWF classes (3) + exam = 4; the admin 'Reading day'
    # on Sep 22 is excluded.
    assert weeks["2026-09-21"] == 4
    conn.close()


def test_ics_import(tmp_path):
    from brain.db import connect

    ics = tmp_path / "cal.ics"
    # Plain \n: text-mode write translates it to the platform line ending.
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        "BEGIN:VEVENT\nUID:1@test\nSUMMARY:TEST101 Final Exam\n"
        "DTSTART:20260924T130000\nDTEND:20260924T150000\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:2@test\nSUMMARY:Advising day\n"
        "DTSTART;VALUE=DATE:20260923\nEND:VEVENT\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.calendar.ics_paths = [ics]
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal.import_calendar(config, conn)
    ics_report = next(s for s in report.sources if s.source == "ics")
    assert ics_report.imported == 2 and not ics_report.errors
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM events WHERE source='ics'")}
    exam = rows["TEST101 Final Exam"]
    assert exam["course"] == "TEST101"
    assert exam["kind"] == "exam"
    assert not exam["all_day"]
    advising = rows["Advising day"]
    assert advising["course"] == "OTHER"
    assert advising["kind"] == "admin"
    assert advising["all_day"]
    conn.close()


def test_missing_ics_reported_loudly(tmp_path):
    from brain.db import connect

    config = _config(tmp_path)
    config.calendar.ics_paths = [tmp_path / "nope.ics"]
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal.import_calendar(config, conn)
    ics_report = next(s for s in report.sources if s.source == "ics")
    assert ics_report.status == "failed"
    assert "does not exist" in ics_report.errors[0]
    conn.close()


def test_ics_rrule_is_expanded_across_the_semester(tmp_path):
    from brain.db import connect

    ics = tmp_path / "rec.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:1@t\nSUMMARY:TEST101 Weekly Quiz\n"
        "DTSTART:20260908T130000\nDTEND:20260908T140000\n"
        "RRULE:FREQ=WEEKLY;COUNT=10\n"
        "EXDATE:20260915T130000\n"
        "END:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)  # semester 2026-09-07 .. 2026-09-25
    config.calendar.ics_paths = [ics]
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal.import_calendar(config, conn)
    ics_report = next(s for s in report.sources if s.source == "ics")
    assert not ics_report.errors
    days = sorted(r["starts_at"][:10]
                  for r in conn.execute("SELECT starts_at FROM events WHERE source='ics'"))
    # Weekly Tuesdays inside the window: 09-08, (09-15 excluded), 09-22.
    assert days == ["2026-09-08", "2026-09-22"]
    assert all(r["kind"] == "quiz"
               for r in conn.execute("SELECT kind FROM events WHERE source='ics'"))
    conn.close()


def test_ics_duration_and_all_day_dtend(tmp_path):
    from brain.db import connect

    ics = tmp_path / "d.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:1@t\nSUMMARY:TEST101 Lecture\n"
        "DTSTART:20260908T100000\nDURATION:PT1H15M\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:2@t\nSUMMARY:Reading day\n"
        "DTSTART;VALUE=DATE:20260909\nDTEND;VALUE=DATE:20260910\nEND:VEVENT\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.calendar.ics_paths = [ics]
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM events WHERE source='ics'")}
    # DURATION resolves the end instead of leaving it NULL.
    assert rows["TEST101 Lecture"]["ends_at"] == "2026-09-08T11:15:00"
    # All-day DTEND is exclusive in RFC 5545: a one-day event ends that day.
    assert rows["Reading day"]["ends_at"] == "2026-09-09T00:00:00"
    conn.close()


def test_homework_titles_classify_as_deadlines_not_admin():
    assert cal.classify_kind("FINC 380 Homework 2 due") == "project"
    assert cal.classify_kind("HW 3") == "project"
    assert cal.classify_kind("Problem Set 4") == "project"
    assert cal.classify_kind("Lab report") == "project"
    assert cal.classify_kind("Midterm Exam") == "exam"
    assert cal.classify_kind("Chapter 5 Quiz") == "quiz"
    assert cal.classify_kind("Office hours moved") == "admin"


def test_all_day_deadline_stays_upcoming_all_day(tmp_path):
    from brain.db import connect

    csv_text = ("course,title,date,start_time,end_time,all_day,kind\n"
                "TEST101,All day quiz,2026-09-10,,,true,quiz\n")
    config = _config(tmp_path, csv_text)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    # 9 AM on the due day: still due today, must not have vanished at 00:01.
    now = datetime(2026, 9, 10, 9, 0)
    assert [e["title"] for e in cal.next_events(conn, now)] == ["All day quiz"]
    assert cal.due_within(conn, now, 7) == 1
    # The next day it is genuinely past.
    assert cal.next_events(conn, datetime(2026, 9, 11, 9, 0)) == []
    conn.close()


def test_csv_rejects_timed_row_without_start_and_backwards_times(tmp_path):
    csv_text = ("course,title,date,start_time,end_time,all_day,kind\n"
                "TEST101,No start,2026-09-10,,,false,quiz\n"
                "TEST101,Backwards,2026-09-11,15:00,14:00,false,exam\n")
    path = tmp_path / "c.csv"
    path.write_text(csv_text, encoding="utf-8")
    events, errors = cal.parse_fixed_csv(path)
    assert events == []
    assert len(errors) == 2
    assert "start_time is empty" in errors[0]
    assert "not after start_time" in errors[1]


def test_week_load_includes_events_before_semester_start(tmp_path):
    from brain.db import connect

    csv_text = (CSV_GOOD + "TEST101,Orientation quiz,2026-08-28,10:00,,false,quiz\n")
    config = _config(tmp_path, csv_text)  # semester starts 2026-09-07
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    weeks = {w["week_start"]: w["count"] for w in cal.week_load(conn, config)}
    assert weeks.get("2026-08-24") == 1  # not silently dropped
    conn.close()


def test_duplicate_rows_are_reported_not_silently_collapsed(tmp_path):
    from brain.db import connect

    dup = CSV_GOOD + "TEST101,Quiz 1,2026-09-08,12:00,,false,quiz\n"
    config = _config(tmp_path, dup)
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal.import_calendar(config, conn)
    csv_report = next(s for s in report.sources if s.source == "csv")
    assert csv_report.imported == 4
    assert csv_report.stored == 3
    assert any("collapsed" in e for e in csv_report.errors)
    n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='csv'").fetchone()["n"]
    assert n == csv_report.stored
    conn.close()


def test_multi_day_event_is_found_by_a_window_that_starts_after_it(tmp_path):
    from brain.db import connect

    ics = tmp_path / "span.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:1@t\nSUMMARY:TEST101 Exam period\n"
        "DTSTART;VALUE=DATE:20260907\nDTEND;VALUE=DATE:20260912\n"
        "END:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.calendar.ics_paths = [ics]
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    # Window opens two days after the event started; it is still running.
    found = cal.events_between(conn, datetime(2026, 9, 9), datetime(2026, 9, 14))
    assert "TEST101 Exam period" in [e["title"] for e in found]
    # ...and it drops out once the window is entirely past it.
    later = cal.events_between(conn, datetime(2026, 9, 14), datetime(2026, 9, 20))
    assert "TEST101 Exam period" not in [e["title"] for e in later]
    conn.close()


def test_deadline_queries(tmp_path):
    from brain.db import connect

    config = _config(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    cal.import_calendar(config, conn)
    now = datetime(2026, 9, 7, 0, 0)
    nxt = cal.next_events(conn, now, limit=8)
    # Deadline kinds only - no recurring class meetings, no admin days.
    assert [e["title"] for e in nxt] == ["Quiz 1", "Big Exam"]
    assert cal.due_within(conn, now, 7) == 1
    assert cal.due_within(conn, now, 30) == 2
    conn.close()


# ---- graded-work detection (the daily plan's "graded" flag) --------------

def test_looks_graded_trusts_explicit_kinds():
    from brain.calendar import looks_graded

    assert looks_graded("Midterm Exam", "exam")
    assert looks_graded("Initial Stock Portfolio", "project")


def test_looks_graded_accepts_quiz_with_title_evidence():
    from brain.calendar import looks_graded

    for title in ("Chapter 2 / Chapter 3 Quiz", "Prueba 1",
                  "Chapter 2 Assignment (on Connect)",
                  "Supersite: 4 activities, est 28m"):
        assert looks_graded(title, "quiz"), title


def test_looks_graded_rejects_the_connector_catch_all():
    """connectors.sites._classify defaults ANY dated item to kind=quiz so real
    deadlines are never dropped. The plan must not then claim a grade depends
    on a posted link or a dated reading."""
    from brain.calendar import looks_graded

    assert not looks_graded("Zoom link posted (details on OAKS)", "quiz")
    assert not looks_graded("Read Chapter 5 (details on OAKS)", "quiz")
    assert not looks_graded("", "quiz")


def test_looks_graded_ignores_non_deadline_kinds():
    from brain.calendar import looks_graded

    assert not looks_graded("Week 3 - Chapter 4", "admin")
    assert not looks_graded("FINC 313 class", "recurring")
