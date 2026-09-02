"""The quizzes/ endpoint pull, quiz-authority merge, retime detection, and
in-place moved apply.

Why these exist (live-observed 2026-08-31): OAKS retimed the Chapter 2/3 quiz
due from 16:00Z to 14:00Z and sync reported 0 moved - reconcile compared dates
only, and calendar/events serves 2-3 identically titled events per quiz where
first-in-response-order (the availability-OPEN time) used to win. The quiz
record's DueDate is the authority; a same-date time change must surface; and
applying a move must rewrite the stale row, not append a duplicate.
"""

from __future__ import annotations

from datetime import date as _date

from brain import sync as syncmod
from brain.connectors import PulledItem, reconcile
from brain.connectors.detect import Change, ExistingEvent
from brain.connectors.sites import ConnectConnector, OaksConnector

TODAY = _date(2026, 8, 31)


def item(course, title, dt, **kw):
    return PulledItem(course=course, title=title, date=dt, site="oaks", **kw)


def ev(course, title, dt, start_time="", source="csv"):
    return ExistingEvent(course=course, title=title, date=dt, source=source,
                         start_time=start_time)


def quiz(name, due=None, start=None, end=None, qid=1, **extra):
    d = {"QuizId": qid, "Name": name}
    if due:
        d["DueDate"] = due
    if start:
        d["StartDate"] = start
    if end:
        d["EndDate"] = end
    d.update(extra)
    return d


def oev(title, start, end=None, eid=1):
    d = {"Title": title, "StartDateTime": start, "CalendarEventId": eid}
    if end:
        d["EndDateTime"] = end
    return d


# ---- parse_quizzes ------------------------------------------------------

def test_parse_quizzes_due_is_authoritative_and_local():
    c = OaksConnector()
    items = c.parse_quizzes(
        [quiz("Chapter 2 / Chapter 3 Quiz",
              start="2026-08-31T13:00:00.000Z",   # opens 09:00 EDT
              due="2026-08-31T14:00:00.000Z")],    # due   10:00 EDT
        "FINC313", today=TODAY)
    assert len(items) == 1
    it = items[0]
    assert it.date == "2026-08-31"
    assert it.start_time == "10:00"               # DueDate, never the open
    assert set(it.known_times) == {"09:00", "10:00"}
    assert it.external_id == "quiz-1"
    assert "OAKS" in it.title                      # ensure_where applied


def test_parse_quizzes_requires_a_real_due_date():
    """A quiz with only an availability window is NOT dated at its close.

    A month-long availability (open 9/1, close 9/30) says nothing about the
    real in-class date, and the calendar event for that quiz does. Emitting
    the close as a deadline would both invent a wrong date and, once the
    merge folded the correctly-dated event into it, rewrite the right one
    away. No DueDate means defer to events.
    """
    c = OaksConnector()
    items = c.parse_quizzes(
        [quiz("Availability Only", start="2026-09-01T13:00:00.000Z",
              end="2026-09-30T16:00:00.000Z", qid=2),
         quiz("No Dates Yet", qid=3)],
        "FINC313", today=TODAY)
    assert items == []


def test_undated_quiz_leaves_its_calendar_event_alone():
    c = OaksConnector()
    events = c.parse_events(
        [oev("Chapter 4 Quiz", "2026-09-10T14:00:00.000Z")], "FINC313",
        today=TODAY)
    quizzes = c.parse_quizzes(
        [quiz("Chapter 4 Quiz", start="2026-09-01T13:00:00.000Z",
              end="2026-09-30T16:00:00.000Z", qid=4)],
        "FINC313", today=TODAY)
    merged = c._merge_quiz_authority(events, quizzes)
    assert len(merged) == 1
    assert merged[0].date == "2026-09-10"      # the event's real date survives
    assert merged[0].start_time == "10:00"


def test_merge_keeps_a_repeat_the_quiz_api_cannot_see():
    """Drip-release: only the released occurrence reaches the quiz API, so
    folding events by title alone would delete the later one outright."""
    c = OaksConnector()
    events = c.parse_events(
        [oev("Prueba de vocabulario", "2026-09-04T17:00:00.000Z", eid=1),
         oev("Prueba de vocabulario", "2026-09-18T17:00:00.000Z", eid=2)],
        "SPAN200", today=TODAY)
    quizzes = c.parse_quizzes(
        [quiz("Prueba de vocabulario", due="2026-09-04T17:00:00.000Z")],
        "SPAN200", today=TODAY)
    merged = c._merge_quiz_authority(events, quizzes)
    assert sorted(i.date for i in merged) == ["2026-09-04", "2026-09-18"]


def test_parse_quizzes_windows_out_stale_shell_dates():
    c = OaksConnector()
    items = c.parse_quizzes(
        [quiz("Old Quiz", due="2026-03-15T16:00:00.000Z", qid=4),      # last spring
         quiz("Ancient Quiz", due="2022-10-01T16:00:00.000Z", qid=5)],  # 2022 shell
        "FINC313", today=TODAY)
    assert items == []


def test_parse_quizzes_window_hi_clamps_to_semester_end():
    c = OaksConnector()
    q = [quiz("Next Term Placeholder", due="2027-02-01T16:00:00.000Z", qid=6)]
    # inside the rolling +240d window, so without a clamp it leaks...
    assert len(c.parse_quizzes(q, "FINC313", today=TODAY)) == 1
    # ...and the semester-end clamp is what stops it.
    assert c.parse_quizzes(q, "FINC313", today=TODAY,
                           window_hi=_date(2026, 12, 29)) == []


def test_parse_quizzes_classifies_and_strips_proctoring_prefix():
    c = OaksConnector()
    items = c.parse_quizzes(
        [quiz("Proctoring Enabled: Final Exam Spring 2026- Requires Respondus "
              "LockDown Browser", due="2026-12-02T15:00:00.000Z", qid=7)],
        "FINC313", today=TODAY)
    assert items[0].kind == "exam"
    assert not items[0].title.lower().startswith("proctoring")
    # title already names Respondus/LockDown -> no "(on OAKS)" appended
    assert "(on OAKS)" not in items[0].title


# ---- quiz-authority merge ----------------------------------------------

def test_merge_drops_calendar_triplets_and_feeds_known_times():
    c = OaksConnector()
    events = c.parse_events(
        [oev("Chapter 2 / Chapter 3 Quiz", "2026-08-31T13:00:00.000Z", eid=1),
         oev("Chapter 2 / Chapter 3 Quiz", "2026-08-31T14:00:00.000Z", eid=2),
         oev("Chapter 2 / Chapter 3 Quiz", "2026-08-31T14:00:00.000Z", eid=3)],
        "FINC313", today=TODAY)
    quizzes = c.parse_quizzes(
        [quiz("Chapter 2 / Chapter 3 Quiz", due="2026-08-31T14:00:00.000Z")],
        "FINC313", today=TODAY)
    merged = c._merge_quiz_authority(events, quizzes)
    assert len(merged) == 1                       # 3 events + 1 quiz -> 1 item
    assert merged[0].start_time == "10:00"        # the quiz DueDate wins
    assert "09:00" in merged[0].known_times


def test_merge_open_event_on_other_date_never_becomes_a_deadline():
    c = OaksConnector()
    events = c.parse_events(
        [oev("Chapter 4 / 5 Quiz", "2026-09-01T13:00:00.000Z", eid=1),   # opens Tue
         oev("Chapter 4 / 5 Quiz", "2026-09-07T22:00:00.000Z", eid=2)],  # due Mon
        "FINC313", today=TODAY)
    quizzes = c.parse_quizzes(
        [quiz("Chapter 4 / 5 Quiz", start="2026-09-01T13:00:00.000Z",
              due="2026-09-07T22:00:00.000Z")],
        "FINC313", today=TODAY)
    merged = c._merge_quiz_authority(events, quizzes)
    assert len(merged) == 1
    assert merged[0].date == "2026-09-07"         # the open date left no row


def test_merge_collapses_nonquiz_same_title_same_date_to_latest_time():
    c = OaksConnector()
    events = c.parse_events(
        [oev("Reading Response 3", "2026-09-02T13:00:00.000Z", eid=1),
         oev("Reading Response 3", "2026-09-02T22:00:00.000Z", eid=2),
         oev("Something Else", "2026-09-03T15:00:00.000Z", eid=3)],
        "FINC389", today=TODAY)
    merged = c._merge_quiz_authority(events, [])
    by_title = {i.title.split(" (")[0]: i for i in merged}
    assert len(merged) == 2
    assert by_title["Reading Response 3"].start_time == "18:00"
    assert "09:00" in by_title["Reading Response 3"].known_times


# ---- retime detection in reconcile -------------------------------------

def test_retime_same_date_new_time_is_a_move():
    r = reconcile(
        [item("FINC313", "Chapter 2 / Chapter 3 Quiz", "2026-08-31",
              start_time="10:00", known_times=("09:00", "10:00"))],
        [ev("FINC313", "Chapter 2 / Chapter 3 Quiz", "2026-08-31",
            start_time="12:00")])
    assert not r.new and len(r.moved) == 1
    m = r.moved[0]
    assert m.old_date == "2026-08-31" and m.old_time == "12:00"
    assert m.item.start_time == "10:00"


def test_stored_window_open_time_is_not_churned():
    # A row stored at the availability-open time (or hand-edited to it) is
    # current: 09:00 is in known_times, so no phantom retime.
    r = reconcile(
        [item("FINC313", "Chapter 2 / Chapter 3 Quiz", "2026-08-31",
              start_time="10:00", known_times=("09:00", "10:00"))],
        [ev("FINC313", "Chapter 2 / Chapter 3 Quiz", "2026-08-31",
            start_time="09:00")])
    assert not r.moved and r.present == 1


def test_recurring_series_never_retimes():
    existing = [ev("SPAN200", "Supersite homework", d, start_time="12:55")
                for d in ("2026-08-24", "2026-08-26", "2026-08-28")]
    r = reconcile([item("SPAN200", "Supersite homework", "2026-08-26",
                        start_time="13:30")], existing)
    assert not r.moved and r.present == 1


def test_past_deadline_is_never_retimed():
    # Live-observed: a taken FINC389 quiz's dropbox close time (16:00)
    # differed from the stored in-class window start (15:00) - flagging
    # that is churn on history, not news.
    r = reconcile(
        [item("FINC389", "Quiz 1 - Excel Basics", "2026-08-25",
              start_time="16:00")],
        [ev("FINC389", "Quiz 1 - Excel Basics", "2026-08-25",
            start_time="15:00")],
        today="2026-08-31")
    assert not r.moved and r.present == 1
    # ...but the same delta on an upcoming deadline still surfaces.
    r2 = reconcile(
        [item("FINC389", "Quiz 2 - Formulas", "2026-09-08",
              start_time="16:00")],
        [ev("FINC389", "Quiz 2 - Formulas", "2026-09-08",
            start_time="15:00")],
        today="2026-08-31")
    assert len(r2.moved) == 1 and r2.moved[0].old_time == "15:00"


def test_untimed_stored_row_is_not_retimed():
    r = reconcile(
        [item("FINC315", "Connect: Chapter 2", "2026-08-31", start_time="23:59")],
        [ev("FINC315", "Connect: Chapter 2", "2026-08-31", start_time="")])
    assert not r.moved and r.present == 1


# ---- in-place apply -----------------------------------------------------

_HEADER = "course,title,date,start_time,end_time,all_day,kind\n"


def test_apply_retime_rewrites_row_in_place(tmp_path):
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER +
                 "FINC313,Chapter 2 / Chapter 3 Quiz,2026-08-31,12:00,,false,quiz\n",
                 encoding="utf-8")
    ch = Change(item=item("FINC313", "Chapter 2 / Chapter 3 Quiz (on OAKS)",
                          "2026-08-31", start_time="10:00", kind="quiz"),
                kind="moved", old_date="2026-08-31", old_time="12:00")
    n = syncmod._apply_changes(str(p), [ch])
    assert n == 1
    body = p.read_text(encoding="utf-8")
    assert body.count("Chapter 2 / Chapter 3 Quiz") == 1     # no duplicate row
    assert "2026-08-31,10:00" in body
    assert "12:00" not in body
    assert "Chapter 2 / Chapter 3 Quiz,2026" in body          # stored title kept


def test_apply_date_move_rewrites_row_in_place(tmp_path):
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER + "FINC389,Quiz 1,2026-09-01,18:00,,false,quiz\n",
                 encoding="utf-8")
    ch = Change(item=item("FINC389", "Quiz 1 (details on OAKS)", "2026-09-03",
                          start_time="18:00", kind="quiz"),
                kind="moved", old_date="2026-09-01")
    assert syncmod._apply_changes(str(p), [ch]) == 1
    body = p.read_text(encoding="utf-8")
    assert body.count("Quiz 1") == 1
    assert "2026-09-03" in body and "2026-09-01" not in body


def test_apply_move_without_matching_row_falls_back_to_append(tmp_path):
    # The old occurrence lived in a recurring rule / ics feed, not fixed.csv.
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER, encoding="utf-8")
    ch = Change(item=item("SPAN200", "Prueba 2", "2026-09-10",
                          start_time="13:00", kind="quiz"),
                kind="moved", old_date="2026-09-08")
    assert syncmod._apply_changes(str(p), [ch]) == 1
    assert "Prueba 2,2026-09-10" in p.read_text(encoding="utf-8")


def test_three_duplicate_events_keep_the_open_time(tmp_path):
    """Collapsing 3+ duplicates must union both sides' known_times, or the
    earliest (availability-open) time falls out and a row stored at it fires
    a phantom retime - order-dependently, which is worse than consistently."""
    c = OaksConnector()
    raw = [oev("Reading Response 3", "2026-09-02T12:00:00.000Z", eid=1),
           oev("Reading Response 3", "2026-09-02T18:00:00.000Z", eid=2),
           oev("Reading Response 3", "2026-09-03T03:59:00.000Z", eid=3)]
    for order in (raw, list(reversed(raw))):
        merged = c._merge_quiz_authority(
            c.parse_events(order, "FINC389", today=TODAY), [])
        same_day = [i for i in merged if i.date == "2026-09-02"]
        assert {"08:00", "14:00"} <= set(same_day[0].known_times)
        r = reconcile([i for i in merged if i.date == "2026-09-02"],
                      [ev("FINC389", "Reading Response 3", "2026-09-02",
                          start_time="08:00")], today="2026-08-31")
        assert not r.moved            # stored open time is still vouched for


def test_all_day_row_is_never_retimed(tmp_path):
    """An all-day row is stored at midnight, but that 00:00 is a placeholder.
    Treating it as a set time made every timed platform item look like a
    retime of it - and apply could not match the row, so it appended a
    duplicate instead."""
    r = reconcile(
        [item("SPAN200", "Withdraw deadline", "2026-10-22", start_time="12:00")],
        [ExistingEvent(course="SPAN200", title="Withdraw deadline",
                       date="2026-10-22", source="csv", start_time="")],
        today="2026-08-31")
    assert not r.moved and r.present == 1


def test_existing_blanks_all_day_times(tmp_path):
    from brain.db import connect

    conn = connect(tmp_path / "brain.db")
    conn.execute(
        "INSERT INTO events (id, course, title, starts_at, ends_at, all_day,"
        " kind, source) VALUES (?,?,?,?,?,?,?,?)",
        ("a1", "SPAN200", "Withdraw deadline", "2026-10-22T00:00:00", None,
         1, "admin", "csv"))
    conn.execute(
        "INSERT INTO events (id, course, title, starts_at, ends_at, all_day,"
        " kind, source) VALUES (?,?,?,?,?,?,?,?)",
        ("a2", "FINC313", "Chapter 8 Quiz", "2026-09-26T12:00:00", None,
         0, "quiz", "csv"))
    conn.commit()
    by_title = {e.title: e for e in syncmod._existing(conn)}
    assert by_title["Withdraw deadline"].start_time == ""
    assert by_title["Chapter 8 Quiz"].start_time == "12:00"
    conn.close()


def test_two_sources_disagreeing_on_time_do_not_flip_flop():
    """A dropbox close (23:59) and its calendar event (23:00) describe one
    deadline. Judging per item would retime the row to one, then the other,
    forever; the union of what the sources vouch for settles it."""
    pulled = [item("FINC389", "Portfolio Memo", "2026-09-15", start_time="23:59"),
              item("FINC389", "Portfolio Memo", "2026-09-15", start_time="23:00")]
    r = reconcile(pulled, [ev("FINC389", "Portfolio Memo", "2026-09-15",
                              start_time="23:00")], today="2026-08-31")
    assert not r.moved and r.present == 2


def test_one_moved_change_per_slot():
    pulled = [item("FINC313", "Chapter 9 Quiz", "2026-10-05", start_time="10:00",
                   known_times=("10:00",)),
              item("FINC313", "Chapter 9 Quiz", "2026-10-05", start_time="10:00",
                   known_times=("10:00",))]
    r = reconcile(pulled, [ev("FINC313", "Chapter 9 Quiz", "2026-10-05",
                              start_time="12:00")], today="2026-08-31")
    assert len(r.moved) == 1


def test_corroborated_date_is_not_a_move():
    """Two occurrences of one title: the stored date is still served by the
    source, so the second is an ADDITIONAL deadline, not a move of the first.
    Calling it a move would rewrite a correct date away."""
    pulled = [item("SPAN200", "Prueba de vocabulario", "2026-09-04",
                   start_time="13:00"),
              item("SPAN200", "Prueba de vocabulario", "2026-09-18",
                   start_time="13:00")]
    r = reconcile(pulled, [ev("SPAN200", "Prueba de vocabulario", "2026-09-04",
                              start_time="13:00")], today="2026-08-31")
    assert not r.moved and len(r.new) == 1
    assert r.new[0].item.date == "2026-09-18"


def test_course_name_alone_is_not_a_cross_wording_match():
    """Live false positive: OAKS's one-off "FINC 313 Lecture" (11:00) was
    matched to the recurring "FINC 313 class (Prof. A)" (09:00) because they
    share the word "finc" and the number "313" - which every row in the
    course shares. It reported the class meeting as a retimed lecture."""
    from brain.connectors.detect import same_deadline

    assert not same_deadline("FINC313", "FINC 313 Lecture",
                             "FINC313", "FINC 313 class (Prof. A)")
    # the genuine cross-wording case still matches
    assert same_deadline("FINC315", "Chapter 2 Assignment",
                         "FINC315", "Connect: Chapter 2")
    r = reconcile(
        [item("FINC313", "FINC 313 Lecture (details on OAKS)", "2026-09-18",
              start_time="11:00")],
        [ev("FINC313", "FINC 313 class (Prof. A)", "2026-09-18",
            start_time="09:00", source="recurring")],
        today="2026-08-31")
    assert not r.moved


def test_recurring_and_ics_rows_never_retime():
    """Sync cannot edit a recurring rule or an ics feed, so reporting one as
    moved would be noise that nothing could ever clear."""
    r = reconcile(
        [item("SPAN200", "Prueba de verbos 2", "2026-09-10", start_time="13:30")],
        [ev("SPAN200", "Prueba de verbos 2", "2026-09-10",
            start_time="13:00", source="recurring")],
        today="2026-08-31")
    assert not r.moved and r.present == 1


def test_apply_refuses_when_two_rows_match(tmp_path):
    """Duplicates left by the old append-on-move behavior: editing one and
    stranding the other is worse than reporting it."""
    p = tmp_path / "fixed.csv"
    dup = "FINC313,Chapter 9 Quiz,2026-10-05,09:00,,false,quiz\n"
    p.write_text(_HEADER + dup + dup, encoding="utf-8")
    ch = Change(item=item("FINC313", "Chapter 9 Quiz", "2026-10-12",
                          start_time="09:00", kind="quiz"),
                kind="moved", old_date="2026-10-05", old_times=("09:00",))
    assert syncmod._apply_changes(str(p), [ch]) == 0
    assert p.read_text(encoding="utf-8").count("2026-10-05") == 2


def test_apply_retime_preserves_hand_set_end_time(tmp_path):
    """The live in-class row is curated as a 09:00-10:00 window; quiz records
    carry no end time, so a retime must not blank it."""
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER +
                 "FINC313,Chapter 2 / Chapter 3 Quiz,2026-09-14,09:00,10:00,false,quiz\n",
                 encoding="utf-8")
    ch = Change(item=item("FINC313", "Chapter 2 / Chapter 3 Quiz", "2026-09-14",
                          start_time="09:30", kind="quiz"),
                kind="moved", old_date="2026-09-14", old_time="09:00",
                old_times=("09:00",))
    assert syncmod._apply_changes(str(p), [ch]) == 1
    assert "2026-09-14,09:30,10:00" in p.read_text(encoding="utf-8")


def test_apply_unmatched_retime_does_not_append_duplicate(tmp_path):
    """A retime whose stored occurrence lives in an ics feed cannot be
    edited; appending would just add a second same-day row."""
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER, encoding="utf-8")
    ch = Change(item=item("FINC313", "Chapter 9 Quiz", "2026-10-05",
                          start_time="10:00", kind="quiz"),
                kind="moved", old_date="2026-10-05", old_time="12:00",
                old_times=("12:00",))
    assert syncmod._apply_changes(str(p), [ch]) == 0
    assert "Chapter 9 Quiz" not in p.read_text(encoding="utf-8")


def test_apply_is_idempotent_without_a_reimport(tmp_path):
    """Applying the same move twice (no calendar rebuild between) must not
    append a source-worded twin of the row it just rewrote."""
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER + "FINC315,Connect: Chapter 2,2026-08-31,23:59,,false,quiz\n",
                 encoding="utf-8")
    ch = Change(item=item("FINC315", "Chapter 2 Assignment (on Connect)",
                          "2026-09-02", start_time="23:59", kind="quiz"),
                kind="moved", old_date="2026-08-31", old_times=("23:59",))
    assert syncmod._apply_changes(str(p), [ch]) == 1
    assert syncmod._apply_changes(str(p), [ch]) == 0
    body = p.read_text(encoding="utf-8")
    assert body.count("2026-09-02") == 1
    assert "Chapter 2 Assignment" not in body      # stored wording kept


def test_apply_backs_up_and_keeps_lf_endings(tmp_path):
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER + "FINC313,Quiz A,2026-09-14,09:00,,false,quiz\n",
                 encoding="utf-8")
    ch = Change(item=item("FINC313", "Quiz A", "2026-09-15", start_time="09:00",
                          kind="quiz"),
                kind="moved", old_date="2026-09-14", old_times=("09:00",))
    syncmod._apply_changes(str(p), [ch])
    assert list(tmp_path.glob("fixed.csv.bak-*"))          # rollback exists
    assert b"\r\n" not in p.read_bytes()                    # stays LF


def test_append_rows_repairs_a_missing_trailing_newline(tmp_path):
    """A hand edit can leave the last line unterminated; appending onto it
    would corrupt both rows and drop a real deadline from the calendar."""
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER + "FINC313,Quiz A,2026-09-14,09:00,,false,quiz",
                 encoding="utf-8")           # no trailing newline
    syncmod._append_rows(str(p), [item("FINC313", "Quiz B", "2026-09-21",
                                       start_time="09:00", kind="quiz")])
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 3
    assert lines[1].endswith("quiz") and lines[2].startswith("FINC313,Quiz B")


def test_ignore_suppresses_both_readds_and_moves(tmp_path, monkeypatch):
    """A muted key must not reach the DESTRUCTIVE path either.

    This started life asserting the opposite - that a mute should still let
    moves through, so a retime of the term's biggest deadline would surface.
    A review pass showed that reasoning was backwards once apply became
    destructive: a muted item arriving as `moved` rewrites a real row's date
    or time in fixed.csv, and the user has no way to stop it. Mute means "do
    not act on this", so it now covers both. Someone who wants the moves back
    deletes the line from calendar/sync_ignore.txt.
    """
    from brain.config import load_config
    from brain.connectors import SessionStore
    from brain.db import connect
    from conftest import write_config

    cal = ('\n[calendar]\nsemester_start = 2026-08-17\n'
           'semester_end = 2026-12-15\nfixed_csv = "calendar/fixed.csv"\n')
    cfg = load_config(write_config(
        tmp_path, [{"name": "FINC313", "assist_level": "full"}], cal))
    (tmp_path / "calendar").mkdir(exist_ok=True)
    (tmp_path / "calendar" / "sync_ignore.txt").write_text(
        "FINC313|final exam spring|2026\n", encoding="utf-8")
    SessionStore(cfg.settings.data_dir).save("oaks", {"d2lSessionVal": "x"})

    # One item shares the ignored key AND matches a row already on the
    # calendar, but on a new date (a genuine move); the other is a fresh
    # stale-shell re-add of the same suppressed key.
    moved = item("FINC313", "Final Exam Spring 2026 (on OAKS)", "2026-12-05",
                 start_time="10:30")
    readd = item("FINC313", "Final Exam Spring 2026 review (on OAKS)",
                 "2026-11-30", start_time="10:30")
    monkeypatch.setattr(OaksConnector, "pull",
                        lambda self, s, c, window_hi=None: [moved, readd])
    conn = connect(tmp_path / "data" / "brain.db")
    conn.execute(
        "INSERT INTO events (id, course, title, starts_at, ends_at, all_day,"
        " kind, source) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "FINC313", "Final Exam Spring 2026 (on OAKS)",
         "2026-12-02T10:30:00", None, 0, "exam", "csv"))
    conn.commit()
    report = syncmod.run(cfg, conn, only="oaks", apply=False)
    site = report.sites[0]
    assert site.ok
    assert site.recon.new == []                  # re-add suppressed
    assert site.recon.moved == []                # and so is the move
    conn.close()


def test_apply_mixed_new_and_moved(tmp_path):
    p = tmp_path / "fixed.csv"
    p.write_text(_HEADER + "FINC313,Chapter 8 Quiz,2026-09-26,12:00,,false,quiz\n",
                 encoding="utf-8")
    changes = [
        Change(item=item("FINC313", "Chapter 8 Quiz", "2026-09-26",
                         start_time="18:00", kind="quiz"),
               kind="moved", old_date="2026-09-26", old_time="12:00"),
        Change(item=item("FINC313", "Chapter 9 Quiz", "2026-10-03",
                         start_time="12:00", kind="quiz"), kind="new"),
    ]
    assert syncmod._apply_changes(str(p), changes) == 2
    body = p.read_text(encoding="utf-8")
    assert body.count("Chapter 8 Quiz") == 1 and "18:00" in body
    assert "Chapter 9 Quiz" in body


# ---- Connect stale-shell window ----------------------------------------

def _connect_payload(assignments):
    return {
        "sections": [{"id": 1, "course": 10, "name": "FINC 315 Fall 2026 MWF"}],
        "courses": [{"id": 10, "name": "Ross Fundamentals | FINC 315"}],
        "sectionAssignments": assignments,
    }


def test_connect_filters_copied_shell_dues():
    c = ConnectConnector()
    data = _connect_payload([
        {"id": 100, "section": 1, "name": "Chapter 2 Assignment",
         "dueDate": "2026-09-01T03:59:00.000Z"},                 # tonight ET
        {"id": 101, "section": 1, "name": "Stale Copied Item",
         "dueDate": "2026-01-01T05:00:00.000Z"},                 # Jan 1 shell
        {"id": 102, "section": 1, "name": "Ancient Item",
         "dueDate": "2022-09-01T03:59:00.000Z"},
    ])
    items = c.parse_assignments(data, ["FINC315"], today=TODAY)
    assert [i.external_id for i in items] == ["100"]
    assert items[0].date == "2026-08-31"


# ---- parse_events window_hi clamp --------------------------------------

def test_parse_events_window_hi_clamp():
    c = OaksConnector()
    events = [oev("Phantom Next-Term Quiz", "2027-02-01T17:00:00.000Z")]
    assert len(c.parse_events(events, "FINC313", today=TODAY)) == 1
    assert c.parse_events(events, "FINC313", today=TODAY,
                          window_hi=_date(2026, 12, 29)) == []


# ---- quiz body text writer ---------------------------------------------

def _quiz_content_setup(tmp_path, monkeypatch, listing):
    from brain.config import load_config
    from brain.connectors import SessionStore
    from conftest import write_config

    cfg = load_config(write_config(
        tmp_path, [{"name": "FINC313", "assist_level": "full"}]))
    SessionStore(cfg.settings.data_dir).save("oaks", {"d2lSessionVal": "x"})
    monkeypatch.setattr(OaksConnector, "list_quiz_content",
                        lambda self, session, courses: listing)
    return cfg


def test_quiz_content_skips_empty_bodies(tmp_path, monkeypatch):
    cfg = _quiz_content_setup(tmp_path, monkeypatch, [
        {"course": "FINC313", "id": "oaks-quiz-1", "name": "Chapter 8 Quiz",
         "due": "2026-09-26 12:00", "attempts": "1 attempt",
         "description": "", "instructions": "", "header": "", "footer": ""},
    ])
    report = syncmod.pull_quiz_content(cfg, apply=True)
    assert report.total == 1 and report.saved == 0 and not report.quizzes
    assert not (tmp_path / "docs" / "FINC313" / "_synced" / "quizzes").exists()


def test_quiz_content_writes_updates_and_is_idempotent(tmp_path, monkeypatch):
    listing = [
        {"course": "FINC313", "id": "oaks-quiz-2", "name": "Chapter 11 Quiz",
         "due": "2026-10-05 12:00", "attempts": "1 attempt",
         "description": "Covers liquidity risk. Bring a calculator.",
         "instructions": "", "header": "", "footer": ""},
    ]
    cfg = _quiz_content_setup(tmp_path, monkeypatch, listing)
    dest = (tmp_path / "docs" / "FINC313" / "_synced" / "quizzes"
            / "chapter-11-quiz.md")

    dry = syncmod.pull_quiz_content(cfg, apply=False)
    assert dry.quizzes[0]["status"] == "new" and not dest.exists()

    applied = syncmod.pull_quiz_content(cfg, apply=True)
    assert applied.saved == 1 and dest.exists()
    body = dest.read_text(encoding="utf-8")
    assert "Chapter 11 Quiz" in body and "liquidity risk" in body
    assert "due 2026-10-05 12:00" in body and "1 attempt" in body

    again = syncmod.pull_quiz_content(cfg, apply=True)
    assert again.saved == 0 and again.quizzes[0]["status"] == "unchanged"

    listing[0]["description"] = "Now also covers duration gap."
    updated = syncmod.pull_quiz_content(cfg, apply=True)
    assert updated.saved == 1 and updated.quizzes[0]["status"] == "updated"
    assert "duration gap" in dest.read_text(encoding="utf-8")
