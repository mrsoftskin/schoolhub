"""Change detection and calendar merge for credentialed assignment sync.

The reconcile logic is the 'did anything new get posted?' core; a false NEW
creates a phantom deadline the user plans around, so matching is tested hard.
"""

from __future__ import annotations

from brain.connectors import PulledItem, reconcile
from brain.connectors.detect import ExistingEvent, _key
from brain import sync as syncmod


def item(course, title, date, **kw):
    return PulledItem(course=course, title=title, date=date, site="oaks", **kw)


def ev(course, title, date, source="csv"):
    return ExistingEvent(course=course, title=title, date=date, source=source)


def test_key_ignores_bracket_notes_and_stopwords():
    a = _key("SPAN200", "Prueba de verbos 1: Preterito (in class)")
    b = _key("SPAN200", "Prueba de verbos 1: Preterito de verbos regulares [note]")
    assert a == b


def test_key_keeps_numbers_distinct():
    assert _key("FINC313", "Chapter 2 Quiz") != _key("FINC313", "Chapter 3 Quiz")


def test_new_item_is_flagged():
    r = reconcile([item("FINC313", "Chapter 9 Quiz", "2026-10-01")],
                  [ev("FINC313", "Chapter 8 Quiz", "2026-09-20")])
    assert len(r.new) == 1 and not r.moved
    assert r.new[0].item.title == "Chapter 9 Quiz"


def test_present_item_is_not_flagged():
    r = reconcile([item("FINC313", "Chapter 8 Quiz", "2026-09-20")],
                  [ev("FINC313", "Chapter 8 Quiz", "2026-09-20")])
    assert not r.new and not r.moved and r.present == 1


def test_moved_item_when_single_existing_date():
    r = reconcile([item("FINC313", "Midterm Exam", "2026-10-01")],
                  [ev("FINC313", "Midterm Exam", "2026-09-28")])
    assert not r.new and len(r.moved) == 1
    assert r.moved[0].old_date == "2026-09-28" and r.moved[0].item.date == "2026-10-01"


def test_recurring_series_new_date_is_new_not_moved():
    existing = [ev("SPAN200", "Supersite homework", d) for d in
                ("2026-08-24", "2026-08-26", "2026-08-28")]
    r = reconcile([item("SPAN200", "Supersite homework", "2026-08-31")], existing)
    assert len(r.new) == 1 and not r.moved  # ambiguous -> new, never a false move


def test_undated_pulled_item_ignored():
    r = reconcile([item("FINC380", "Blended module", None)], [])
    assert not r.new and not r.moved


def test_title_variation_still_matches_existing():
    # what the calendar stores vs what OAKS returns can differ in wording
    r = reconcile(
        [item("SPAN200", "Prueba de verbos 1 - Preterito de verbos regulares", "2026-08-24")],
        [ev("SPAN200", "Prueba de verbos 1: Preterito de verbos regulares (in class)", "2026-08-24")],
    )
    assert not r.new and r.present == 1


def test_append_rows_dedupes_and_skips_undated(tmp_path):
    csv_path = tmp_path / "fixed.csv"
    csv_path.write_text(
        "course,title,date,start_time,end_time,all_day,kind\n"
        "FINC313,Chapter 8 Quiz,2026-09-20,12:00,,false,quiz\n", encoding="utf-8")
    items = [
        item("FINC313", "Chapter 8 Quiz", "2026-09-20"),      # dup -> skip
        item("FINC313", "Chapter 9 Quiz", "2026-10-01", start_time="12:00", kind="quiz"),
        item("FINC380", "Undated thing", None),                # undated -> skip
    ]
    added = syncmod._append_rows(str(csv_path), items)
    assert added == 1
    body = csv_path.read_text(encoding="utf-8")
    assert "Chapter 9 Quiz" in body
    assert body.count("Chapter 8 Quiz") == 1


def test_parse_cookie_blob_forms():
    a = syncmod.parse_cookie_blob("Cookie: d2lSessionVal=abc; d2lSecureSessionVal=def")
    assert a == {"d2lSessionVal": "abc", "d2lSecureSessionVal": "def"}
    b = syncmod.parse_cookie_blob("name1=v1\nname2=v2\n")
    assert b == {"name1": "v1", "name2": "v2"}


def test_session_store_roundtrip(tmp_path):
    from brain.connectors import SessionStore
    store = SessionStore(tmp_path)
    assert not store.has("oaks")
    store.save("oaks", {"d2lSessionVal": "xyz"}, base_url="https://lms.cofc.edu")
    assert store.has("oaks")
    assert store.load("oaks")["cookies"]["d2lSessionVal"] == "xyz"
    assert store.age_hours("oaks") is not None and store.age_hours("oaks") < 1


def test_run_reports_missing_sessions(tmp_path):
    from brain.config import load_config
    from brain.db import connect
    from conftest import write_config
    cfg = load_config(write_config(tmp_path, [{"name": "FINC313", "assist_level": "full"}]))
    conn = connect(tmp_path / "data" / "brain.db")
    report = syncmod.run(cfg, conn, apply=False)
    assert len(report.sites) == 4
    # Never-connected is reported as "not connected", not as a failure the
    # student is expected to repair; `configured` is what the UI branches on.
    assert all(not s.ok and "not connected" in s.error for s in report.sites)
    assert all(s.configured is False for s in report.sites)
    conn.close()


def test_parse_cookie_blob_request_headers_block():
    # A full "Copy request headers" paste: only the Cookie line counts; the
    # referer URL's query string must not be misread as cookies.
    blob = (
        "GET /d2l/home HTTP/2\n"
        "accept: text/html\n"
        "referer: https://lms.cofc.edu/d2l/home?ou=6606&x=1\n"
        "cookie: d2lSessionVal=abc; d2lSecureSessionVal=def\n"
        "user-agent: Mozilla/5.0\n"
    )
    c = syncmod.parse_cookie_blob(blob)
    assert c == {"d2lSessionVal": "abc", "d2lSecureSessionVal": "def"}


def test_parse_cookie_blob_curl_bash():
    blob = (
        "curl 'https://lms.cofc.edu/d2l/home' \\n"
        "  -H 'accept: text/html' \\n"
        "  -H 'cookie: sid=a1b2; token=xyz%3D%3D' \\n"
        "  -H 'user-agent: Mozilla/5.0'\n"
    )
    c = syncmod.parse_cookie_blob(blob)
    assert c == {"sid": "a1b2", "token": "xyz%3D%3D"}


def test_parse_cookie_blob_curl_cmd_caret_escapes():
    # Chrome's "Copy as cURL (cmd)" flavor: ^" quotes, ^% and ^& escapes,
    # caret line continuations.
    blob = (
        'curl ^"https://connect.mheducation.com/^" ^\n'
        '  -H ^"accept: text/html^" ^\n'
        '  -H ^"cookie: MHSession=q1w2e3; pct=US^&campus=1; enc=ab^%^3Dcd^" ^\n'
        '  -H ^"user-agent: Mozilla/5.0^"\n'
    )
    c = syncmod.parse_cookie_blob(blob)
    assert c == {"MHSession": "q1w2e3", "pct": "US&campus=1", "enc": "ab%3Dcd"}


def test_parse_cookie_blob_curl_dash_b():
    c = syncmod.parse_cookie_blob("curl 'https://x.com/' -b 'a=1; b=2'")
    assert c == {"a": "1", "b": "2"}


def test_parse_cookie_blob_curl_without_cookie_header_is_empty():
    c = syncmod.parse_cookie_blob("curl 'https://x.com/' -H 'accept: text/html'")
    assert c == {}


# ---- OAKS connector (real API shapes, captured 2026-08-25) -------------

from brain.connectors.sites import OaksConnector, _classify, _current_term
from datetime import date as _date


def _ev(title, start, *, end=None, all_day=False, eid=1, url="https://x/e"):
    return {"Title": title, "StartDateTime": start,
            "EndDateTime": end or start, "IsAllDayEvent": all_day,
            "CalendarEventId": eid, "CalendarEventViewUrl": url}


def test_oaks_parse_converts_utc_to_local_date():
    # 2026-09-01T03:59Z is 2026-08-31 23:59 EDT - the date the syllabus and
    # fixed.csv use. A UTC-naive parser would put it a day late.
    c = OaksConnector()
    items = c.parse_events([_ev("Chapter 2 Assignment", "2026-09-01T03:59:00.000Z")],
                           "FINC315", today=_date(2026, 8, 25))
    assert len(items) == 1
    it = items[0]
    assert it.date == "2026-08-31"
    assert it.start_time == "23:59"
    assert it.course == "FINC315"
    assert it.site == "oaks"


def test_oaks_parse_filters_reused_shell_history():
    # The live endpoint returns events from 2021-2024 (instructor reuses the
    # course shell); the window filter must drop them.
    c = OaksConnector()
    events = [
        _ev("FINC 303-04 Final Exam Review Session", "2021-08-09T18:00:00.000Z"),
        _ev("Extra Credit Assignment", "2022-11-20T04:59:00.000Z"),
        _ev("Chapter 3 Assignment", "2026-09-08T03:59:00.000Z"),
    ]
    items = c.parse_events(events, "FINC315", today=_date(2026, 8, 25))
    assert [i.title for i in items] == ["Chapter 3 Assignment (details on OAKS)"]


def test_oaks_parse_end_time_and_all_day():
    c = OaksConnector()
    timed = _ev("Exam 1", "2026-09-23T19:00:00.000Z",
                end="2026-09-23T20:15:00.000Z")
    allday = _ev("Reading day", "2026-10-13T00:00:00.000Z", all_day=True)
    items = c.parse_events([timed, allday], "FINC389", today=_date(2026, 8, 25))
    assert items[0].start_time == "15:00" and items[0].end_time == "16:15"
    assert items[1].all_day and items[1].start_time == ""


def test_oaks_map_enrollments_current_term_only():
    c = OaksConnector()
    enr = {"Items": [
        {"OrgUnit": {"Id": 369912, "Type": {"Code": "Course Offering"},
                     "Name": "2025 Fall Business Finance (FINC-303-07)"}},
        {"OrgUnit": {"Id": 400852, "Type": {"Code": "Course Offering"},
                     "Name": "2026 Fall Intermediate Business Finance (FINC-315-01)"}},
        {"OrgUnit": {"Id": 400854, "Type": {"Code": "Group"},
                     "Name": "2026 Fall Intermediate Business Finance (FINC-315-01)"}},
        {"OrgUnit": {"Id": 403260, "Type": {"Code": "Course Offering"},
                     "Name": "2026 Fall Elementary Spanish through Culture II (SPAN-200-04-05-07) 13"}},
    ]}
    courses = ["FINC313", "FINC315", "SPAN200", "REUW", "obsidian"]
    out = c.map_enrollments(enr, courses, today=_date(2026, 8, 25))
    assert out == {400852: "FINC315", 403260: "SPAN200"}


def test_current_term_seasons():
    assert _current_term(_date(2026, 8, 25)) == "2026 Fall"
    assert _current_term(_date(2026, 2, 1)) == "2026 Spring"
    assert _current_term(_date(2026, 6, 15)) == "2026 Summer"


def test_classify_kinds():
    assert _classify("Proctoring Enabled: Exam 1") == "exam"
    assert _classify("Chapter 7 Assignment") == "quiz"
    assert _classify("Final Project Presentations") == "project"
    assert _classify("FINC 315 Office Hours") == "admin"


# ---- cross-wording reconcile match -------------------------------------

def test_reconcile_cross_worded_same_date_is_present():
    # OAKS says "Chapter 2 Assignment"; the calendar says "Connect: Chapter 2"
    # on the same date. Different primary keys, same deadline.
    pulled = [PulledItem(course="FINC315", title="Chapter 2 Assignment",
                         date="2026-08-31", site="oaks")]
    existing = [ExistingEvent(course="FINC315", title="Connect: Chapter 2",
                              date="2026-08-31")]
    r = reconcile(pulled, existing)
    assert r.present == 1 and not r.new and not r.moved


def test_reconcile_cross_worded_needs_shared_word():
    # Same course, same number, same date, but NO shared content word:
    # that is a different item and must stay new.
    pulled = [PulledItem(course="FINC315", title="Homework 2",
                         date="2026-08-31", site="oaks")]
    existing = [ExistingEvent(course="FINC315", title="Connect: Chapter 2",
                              date="2026-08-31")]
    r = reconcile(pulled, existing)
    assert len(r.new) == 1 and r.present == 0


def test_classify_week_topics_and_materials_are_admin():
    assert _classify("Week 3 - Chapter 4 The Fed, Monetary Policy") == "admin"
    assert _classify("Week 12: Midterm and Chapter 18 Pension Plans") == "admin"
    assert _classify("Exam 2 Sample Questions") == "admin"
    assert _classify("FINC 313 Lecture") == "admin"
    # but a midterm NOT inside a week-topic title stays an exam
    assert _classify("Midterm Exam #1: Chapters 1-8") == "exam"


def test_oaks_parse_strips_proctoring_prefix():
    c = OaksConnector()
    items = c.parse_events(
        [_ev("Proctoring Enabled: Final Exam", "2026-12-02T04:59:00.000Z")],
        "FINC315", today=_date(2026, 11, 20))
    assert items[0].title == "Final Exam (details on OAKS)"
    assert items[0].kind == "exam"


def test_oaks_parse_skips_recurring_class_meetings():
    ev = _ev("FINC 313 Lecture", "2026-09-07T14:00:00.000Z")
    ev["IsRecurring"] = True
    c = OaksConnector()
    assert c.parse_events([ev], "FINC313", today=_date(2026, 8, 25)) == []


# ---- "where do I do this" labeling -------------------------------------

from brain.connectors.base import ensure_where


def test_ensure_where_appends_only_when_missing():
    assert ensure_where("Quiz 1 - Excel Basics", "details on OAKS") == \
        "Quiz 1 - Excel Basics (details on OAKS)"
    # titles that already say where stay untouched
    assert ensure_where("Connect: Chapter 2", "details on OAKS") == "Connect: Chapter 2"
    assert ensure_where("Prueba de verbos 2 (in class)", "OAKS") == \
        "Prueba de verbos 2 (in class)"
    assert ensure_where("EXAM 1 (proctored - LockDown)", "OAKS") == \
        "EXAM 1 (proctored - LockDown)"


def test_oaks_calendar_items_get_where_label():
    c = OaksConnector()
    items = c.parse_events([_ev("Quiz 1 - Excel Basics", "2026-08-25T19:00:00.000Z")],
                           "FINC389", today=_date(2026, 8, 25))
    assert items[0].title == "Quiz 1 - Excel Basics (details on OAKS)"


def test_oaks_dropbox_parses_as_submit_on_oaks():
    c = OaksConnector()
    folders = [
        {"Id": 7, "Name": "Initial Stock Portfolio", "DueDate": "2026-08-27T03:59:59.000Z"},
        {"Id": 8, "Name": "Time Value of Money Problems", "DueDate": None},
    ]
    items = c.parse_dropbox(folders, "FINC389", today=_date(2026, 8, 25))
    assert len(items) == 1   # undated dropbox skipped
    it = items[0]
    assert it.title == "Initial Stock Portfolio (submit on OAKS)"
    assert it.date == "2026-08-26" and it.start_time == "23:59"
    assert it.kind == "project" and it.external_id == "dropbox-7"


def test_where_label_is_invisible_to_the_matcher():
    # The suffix must never make an already-tracked item look new.
    pulled = [PulledItem(course="FINC389", title="Initial Stock Portfolio (submit on OAKS)",
                         date="2026-08-26", site="oaks")]
    existing = [ExistingEvent(course="FINC389", title="Initial Stock Portfolio",
                              date="2026-08-26")]
    r = reconcile(pulled, existing)
    assert r.present == 1 and not r.new


def test_sync_ignore_file_suppresses_item(tmp_path, monkeypatch):
    from brain.connectors.detect import _key as keyfn
    from brain import sync as s

    class FakeCal:
        fixed_csv = tmp_path / "fixed.csv"
    class FakeCfg:
        calendar = FakeCal()
    (tmp_path / "sync_ignore.txt").write_text(
        "# comment\n" + keyfn("FINC313", "Final Exam Spring 2026- Requires Respondus") + "\n",
        encoding="utf-8")
    ig = s._load_ignore(FakeCfg())
    assert len(ig) == 1
    assert keyfn("FINC313", "Final Exam Spring 2026- Requires Respondus LockDown Browser") in ig


# ---- VHL connector ------------------------------------------------------

from brain.connectors.sites import VhlConnector

VHL_HTML = '''<div class="js-student-dashboard-app"
 data-calendar-url="/courses/1/sections/2/study_schedule"
 data-assignment-summaries="[{&quot;activities_remaining&quot;:4,&quot;assignment_count&quot;:4,&quot;day&quot;:&quot;26&quot;,&quot;detail_url&quot;:&quot;/courses/1/sections/2/assignments_by_due_date?due_date=2026-08-26&quot;,&quot;due_date&quot;:&quot;2026-08-26&quot;,&quot;estimated_time&quot;:&quot;28m&quot;,&quot;incomplete&quot;:true},{&quot;assignment_count&quot;:10,&quot;due_date&quot;:&quot;2026-12-04&quot;,&quot;estimated_time&quot;:&quot;1h 10m&quot;}]"
></div>'''


def test_vhl_parses_embedded_summaries():
    items = VhlConnector().parse_page(VHL_HTML)
    assert len(items) == 2
    a, b = items
    assert a.course == "SPAN200" and a.date == "2026-08-26"
    assert a.title == "Supersite: 4 activities, est 28m"
    assert a.start_time == "12:55" and a.site == "vhl"
    assert b.title == "Supersite: 10 activities, est 1h 10m"


def test_vhl_resolves_course_from_configured_language_class():
    """A friend's VHL deadlines land in THEIR language course, not a hardcoded
    SPAN200 - the course is whichever configured collection is a language dept."""
    v = VhlConnector()
    assert v._resolve_course(["FINC313", "FREN102", "BIOL101"]) == "FREN102"
    assert v._resolve_course(["finc-313", "span 200"]) == "span 200"
    # nothing language-y configured -> the default, not a crash
    assert v._resolve_course(["FINC313", "BIOL101"]) == "SPAN200"
    assert v._resolve_course([]) == "SPAN200"


def test_vhl_parse_page_uses_resolved_course():
    items = VhlConnector().parse_page(VHL_HTML, "FREN102")
    assert items and all(i.course == "FREN102" for i in items)


def test_vhl_day_matches_generic_supersite_row():
    # The calendar's recurring generic row covers the same daily bucket.
    pulled = [PulledItem(course="SPAN200", title="Supersite: 4 activities, est 28m",
                         date="2026-08-26", site="vhl")]
    existing = [ExistingEvent(course="SPAN200",
                              title="VHL Supersite homework (due before class)",
                              date="2026-08-26")]
    r = reconcile(pulled, existing)
    assert r.present == 1 and not r.new


def test_vhl_day_without_generic_row_is_new():
    # Dec 4 falls after classes end - no recurring row - a genuine find.
    pulled = [PulledItem(course="SPAN200", title="Supersite: 10 activities, est 1h 10m",
                         date="2026-12-04", site="vhl")]
    existing = [ExistingEvent(course="SPAN200",
                              title="VHL Supersite homework (due before class)",
                              date="2026-12-02")]
    r = reconcile(pulled, existing)
    assert len(r.new) == 1 and r.present == 0


def test_vhl_bucket_rule_does_not_leak_to_other_sites():
    # An OAKS item sharing a date with a Supersite row must still be new.
    pulled = [PulledItem(course="SPAN200", title="Ensayo final",
                         date="2026-08-26", site="oaks")]
    existing = [ExistingEvent(course="SPAN200",
                              title="VHL Supersite homework (due before class)",
                              date="2026-08-26")]
    r = reconcile(pulled, existing)
    assert len(r.new) == 1


def test_vhl_repeated_counts_do_not_phantom_move():
    # 9/9 and 12/4 both have "10 activities" - identical primary keys. The
    # 9/9 day is covered by a generic row and must be present, not a "move"
    # of the 12/4 row.
    pulled = [PulledItem(course="SPAN200", title="Supersite: 10 activities, est 2h 14m",
                         date="2026-09-09", site="vhl")]
    existing = [
        ExistingEvent(course="SPAN200", title="VHL Supersite homework (due before class)",
                      date="2026-09-09"),
        ExistingEvent(course="SPAN200", title="Supersite: 10 activities, est 1h 10m",
                      date="2026-12-04"),
    ]
    r = reconcile(pulled, existing)
    assert r.present == 1 and not r.moved and not r.new


# ---- OAKS course-file listing ------------------------------------------

def test_walk_toc_flattens_only_file_topics():
    toc = {"Modules": [
        {"Title": "Chapter 2", "Topics": [
            {"TypeIdentifier": "File", "Identifier": 11, "Title": "Ch2 Slides",
             "Url": "/content/enforced/x/Chapter 2 Slides.pdf"},
            {"TypeIdentifier": "Link", "Identifier": 12, "Title": "Ch2 Assignment",
             "Url": "/d2l/quickLink"},
        ], "Modules": [
            {"Title": "Sub", "Topics": [
                {"TypeIdentifier": "File", "Identifier": 13, "Title": "Notes",
                 "Url": "/content/enforced/x/Ch 2 Notes.docx"},
            ]},
        ]},
    ]}
    files = OaksConnector().walk_toc(toc, "FINC315", 400852)
    names = {f["filename"] for f in files}
    assert names == {"Chapter 2 Slides.pdf", "Ch 2 Notes.docx"}   # Link excluded
    slides = next(f for f in files if f["filename"] == "Chapter 2 Slides.pdf")
    assert slides["course"] == "FINC315" and slides["ou"] == 400852
    assert slides["module_path"] == "Chapter 2"
    notes = next(f for f in files if f["filename"] == "Ch 2 Notes.docx")
    assert notes["module_path"] == "Chapter 2 > Sub"


def test_walk_toc_sanitizes_windows_hostile_names():
    toc = {"Modules": [{"Title": "M", "Topics": [
        {"TypeIdentifier": "File", "Identifier": 1, "Title": "x",
         "Url": "/content/enforced/x/Q1: A<b>test?.pdf"},
    ]}]}
    files = OaksConnector().walk_toc(toc, "FINC313", 1)
    assert files[0]["filename"] == "Q1_ A_b_test_.pdf"


def test_pull_files_skips_present_and_flags_new(tmp_path, monkeypatch):
    from brain import sync as s
    from brain.connectors import sites

    root = tmp_path / "FINC315"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "Have.pdf").write_bytes(b"x")   # already on disk

    class Col:
        name = "FINC315"; roots = [str(root)]
    class Cfg:
        collections = [Col()]
        def collection_names(self): return ["FINC315"]
        class settings: data_dir = str(tmp_path / "data")

    listing = [
        {"course": "FINC315", "ou": 1, "topic_id": 10, "title": "Have",
         "filename": "Have.pdf", "module_path": "M"},
        {"course": "FINC315", "ou": 1, "topic_id": 11, "title": "New",
         "filename": "New.pdf", "module_path": "M"},
    ]
    monkeypatch.setattr(sites.OaksConnector, "list_files",
                        lambda self, sess, courses: listing)
    monkeypatch.setattr(s.SessionStore, "has", lambda self, n: n == "oaks")
    monkeypatch.setattr(s.SessionStore, "load", lambda self, n: {"cookies": {}})

    rep = s.pull_files(Cfg(), only="oaks", apply=False)
    by_name = {f.filename: f.status for f in rep.files}
    assert by_name["Have.pdf"] == "skipped"
    assert by_name["New.pdf"] == "downloaded"   # dry-run "would download"
    assert rep.downloaded == 1 and rep.skipped == 1


# ---- announcements (OAKS news) -----------------------------------------

def test_parse_news_filters_hidden_and_shapes():
    c = OaksConnector()
    news = [
        {"Id": 1, "Title": "First Quiz on Tuesday!", "IsPublished": True,
         "StartDate": "2026-08-20T12:00:00.000Z",
         "Body": {"Text": "Quiz is on Excel basics."}},
        {"Id": 2, "Title": "Hidden draft", "IsHidden": True,
         "Body": {"Text": "nope"}},
    ]
    out = c.parse_news(news, "FINC389")
    assert len(out) == 1
    n = out[0]
    assert n["id"] == "oaks-1" and n["course"] == "FINC389"
    assert n["date"] == "2026-08-20" and "Excel basics" in n["text"]


def test_check_news_seen_tracking_and_apply(tmp_path, monkeypatch):
    import json
    from brain import sync as s
    from brain.connectors import sites

    root = tmp_path / "FINC389"; root.mkdir()

    class Col: name = "FINC389"; roots = [str(root)]
    class Cfg:
        collections = [Col()]
        def collection_names(self): return ["FINC389"]
        class settings: data_dir = tmp_path

    items = [{"course": "FINC389", "id": "oaks-1", "title": "First Quiz on Tuesday!",
              "date": "2026-08-20", "text": "Quiz on Excel basics."}]
    monkeypatch.setattr(sites.OaksConnector, "list_news",
                        lambda self, sess, courses: items)
    monkeypatch.setattr(s.SessionStore, "has", lambda self, n: n == "oaks")
    monkeypatch.setattr(s.SessionStore, "load", lambda self, n: {"cookies": {}})

    # dry run: reported new, NOT marked seen
    r1 = s.check_news(Cfg(), apply=False)
    assert len(r1.new) == 1 and r1.saved == 0
    r2 = s.check_news(Cfg(), apply=False)
    assert len(r2.new) == 1              # still unread

    # apply: written to _synced/announcements and marked seen
    r3 = s.check_news(Cfg(), apply=True)
    assert r3.saved == 1
    files = list((root / "_synced" / "announcements").glob("*.md"))
    assert len(files) == 1 and "first-quiz-on-tuesday" in files[0].name
    body = files[0].read_text(encoding="utf-8")
    assert "Excel basics" in body and "FINC389" in body

    # after apply: no longer new
    r4 = s.check_news(Cfg(), apply=False)
    assert r4.new == [] and r4.total == 1


def test_session_save_keeps_an_existing_base_url(tmp_path):
    """VHL's base_url IS its section URL and the connector dies without it.
    `brain sync login vhl` saves cookies only, so a caller that omits
    base_url must not clear it - re-logging in used to destroy the URL, and
    the error it produced told the user to re-log in, which never fixed it.
    """
    from brain.connectors import SessionStore

    store = SessionStore(tmp_path)
    url = "https://m3a.vhlcentral.com/courses/1000000/sections/2000000"
    store.save("vhl", {"a": "1"}, base_url=url)
    store.save("vhl", {"a": "2"})                     # a re-login: cookies only
    assert store.load("vhl")["base_url"] == url
    assert store.load("vhl")["cookies"]["a"] == "2"   # cookies still refreshed


def test_session_save_accepts_a_new_base_url(tmp_path):
    from brain.connectors import SessionStore

    store = SessionStore(tmp_path)
    store.save("vhl", {"a": "1"}, base_url="https://old/sections/1")
    store.save("vhl", {"a": "1"}, base_url="https://new/sections/2")
    assert store.load("vhl")["base_url"] == "https://new/sections/2"
