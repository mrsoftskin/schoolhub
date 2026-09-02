"""Calendar subsystem: import ICS files, the fixed CSV, and recurring rules
into the events table; compute week-load.

Import is idempotent: event ids are content hashes, and a source is fully
rebuilt (DELETE + INSERT, so removed items disappear) ONLY when every one of
its inputs parsed with zero errors. If anything failed - an unreadable ICS,
a bad CSV row - that source is upserted instead of rebuilt, so previously
imported events are never destroyed by a broken input, and the failure is
reported loudly. Stale events can then linger; the report says so.

CSV schema (header required):
  course,title,date,start_time,end_time,all_day,kind
  date YYYY-MM-DD; times HH:MM 24h; all_day true/false; kind one of
  exam|project|quiz|admin. A row with all_day=false must carry a start_time.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from sqlite3 import Connection

from . import feeds
from .config import CalendarConfig, Config
from .errors import BrainError

DEADLINE_KINDS = ("exam", "project", "quiz")
MAX_OCCURRENCES = 500  # per RRULE, guards against unbounded rules


@dataclass
class Event:
    id: str
    course: str
    title: str
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    kind: str
    source: str


@dataclass
class SourceReport:
    source: str            # 'ics', 'csv', or 'recurring'
    detail: str            # file path or 'rules'
    imported: int = 0      # events parsed from this input
    stored: int = 0        # rows actually written (after id de-duplication)
    errors: list[str] = field(default_factory=list)
    parsed: bool = True    # False = input could not be read/parsed at all

    @property
    def status(self) -> str:
        if not self.parsed:
            return "failed"
        return "partial" if self.errors else "ok"


@dataclass
class CalendarImportReport:
    sources: list[SourceReport] = field(default_factory=list)
    full_rebuild: dict[str, bool] = field(default_factory=dict)

    @property
    def total_imported(self) -> int:
        return sum(s.imported for s in self.sources)

    @property
    def total_stored(self) -> int:
        return sum(s.stored for s in self.sources)

    @property
    def all_errors(self) -> list[str]:
        return [f"{s.detail}: {e}" for s in self.sources for e in s.errors]

    def upsert_only(self) -> list[str]:
        """Sources that were upserted rather than rebuilt, so deletions made
        at the source may still be present in the database."""
        return [src for src, full in self.full_rebuild.items() if not full]


def _event_id(source: str, course: str, title: str, starts_at: datetime) -> str:
    key = f"{source}|{course}|{title}|{starts_at.isoformat()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def make_event(
    source: str, course: str, title: str, starts_at: datetime,
    ends_at: datetime | None, all_day: bool, kind: str,
) -> Event:
    return Event(
        id=_event_id(source, course, title, starts_at),
        course=course, title=title, starts_at=starts_at, ends_at=ends_at,
        all_day=all_day, kind=kind, source=source,
    )


# ------------------------------------------------------------------ parsing

_EXAM_RE = re.compile(r"\b(exam|final|midterm|examen)\b", re.I)
_QUIZ_RE = re.compile(r"\b(quiz|prueba|test)\b", re.I)
_PROJECT_RE = re.compile(
    r"\b(project|paper|presentation|proyecto|assignment|essay|homework|hw|"
    r"problem\s+set|pset|lab|report|reading|discussion|tarea|deberes)\b",
    re.I,
)


def classify_kind(title: str) -> str:
    """Infer an event kind from a title (ICS only; CSV rows carry an explicit
    kind). Anything unrecognized is 'admin', which is excluded from workload
    and deadline views - so the graded-work patterns above are deliberately
    broad."""
    if _EXAM_RE.search(title):
        return "exam"
    if _QUIZ_RE.search(title):
        return "quiz"
    if _PROJECT_RE.search(title):
        return "project"
    return "admin"


# Evidence that a row is actual graded coursework. Needed because the SITE
# connectors deliberately default ANY dated item to kind="quiz" (better a
# spurious deadline than a dropped one - see connectors.sites._classify), so
# kind alone over-claims: a dated reading or a posted link would count as
# graded work in the daily plan.
_GRADED_TITLE_RE = re.compile(
    r"\b(quiz|prueba|test|exam|examen|final|midterm|project|proyecto|paper|"
    r"essay|presentation|assignment|homework|hw|tarea|deberes|problem\s+set|"
    r"pset|lab|report|draft|discussion|submission|dropbox|activities)\b",
    re.I,
)


def looks_graded(title: str, kind: str) -> bool:
    """Is this row actually graded coursework?

    'exam' and 'project' are only ever assigned from explicit title evidence,
    so they are trusted. 'quiz' is also the connectors' catch-all for anything
    dated, so it needs the title to back it up before the plan claims a grade
    depends on it. Everything else (admin, recurring) is not graded work.
    """
    if kind in ("exam", "project"):
        return True
    if kind != "quiz":
        return False
    return bool(_GRADED_TITLE_RE.search(title or ""))


def _match_course(text: str, known_courses: list[str]) -> str:
    compact = re.sub(r"[\s_-]+", "", text).upper()
    for c in known_courses:
        if re.sub(r"[\s_-]+", "", c).upper() in compact:
            return c
    return "OTHER"


def _to_local_naive(value) -> tuple[datetime, bool]:
    """icalendar DTSTART/DTEND -> (naive local datetime, is_all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0)), True
    raise BrainError(f"Unsupported ICS date value: {value!r}")


def _ics_end(component, starts_at: datetime, all_day: bool) -> datetime | None:
    """Resolve the end of a VEVENT from DTEND or DURATION.

    For all-day events DTEND is exclusive per RFC 5545 (a one-day event on
    Oct 12 carries DTEND Oct 13), so it is pulled back to the last day the
    event actually covers.
    """
    dtend = component.get("DTEND")
    if dtend is not None:
        ends_at, _ = _to_local_naive(dtend.dt)
        if all_day:
            ends_at -= timedelta(days=1)
            if ends_at < starts_at:
                ends_at = starts_at
        return ends_at
    duration = component.get("DURATION")
    if duration is not None:
        delta = duration.dt
        if not isinstance(delta, timedelta):
            raise BrainError(f"Unsupported DURATION value: {duration.dt!r}")
        return starts_at + delta
    return None


def _expand_rrule(component, starts_at: datetime, window: tuple[date, date]) -> list[datetime]:
    """Expand RRULE/RDATE/EXDATE into concrete start datetimes inside the
    window. Returns [starts_at] when the event does not recur."""
    if component.get("RRULE") is None and component.get("RDATE") is None:
        return [starts_at]

    from dateutil.rrule import rrulestr

    lines: list[str] = []
    rrule_prop = component.get("RRULE")
    if rrule_prop is not None:
        for prop in (rrule_prop if isinstance(rrule_prop, list) else [rrule_prop]):
            lines.append(f"RRULE:{prop.to_ical().decode('utf-8')}")
    rule = rrulestr("\n".join(lines), dtstart=starts_at, forceset=True) if lines else None

    def _dates(prop_name: str) -> list[datetime]:
        prop = component.get(prop_name)
        if prop is None:
            return []
        out: list[datetime] = []
        for p in (prop if isinstance(prop, list) else [prop]):
            for d in p.dts:
                dt, _ = _to_local_naive(d.dt)
                out.append(dt)
        return out

    if rule is None:
        from dateutil.rrule import rruleset

        rule = rruleset()
    for dt in _dates("RDATE"):
        rule.rdate(dt)
    for dt in _dates("EXDATE"):
        rule.exdate(dt)

    win_start = datetime.combine(window[0], time(0, 0))
    win_end = datetime.combine(window[1], time(23, 59, 59))
    occurrences = list(rule.between(win_start, win_end, inc=True))[:MAX_OCCURRENCES]
    return occurrences


def parse_ics_file(
    path: Path, known_courses: list[str], window: tuple[date, date]
) -> tuple[list[Event], list[str]]:
    from icalendar import Calendar

    events: list[Event] = []
    errors: list[str] = []
    cal = Calendar.from_ical(path.read_bytes())
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip() or "(untitled)"
        try:
            dtstart = component.get("DTSTART")
            if dtstart is None:
                errors.append(f"VEVENT '{summary}' has no DTSTART; skipped")
                continue
            starts_at, all_day = _to_local_naive(dtstart.dt)
            ends_at = _ics_end(component, starts_at, all_day)
            duration = (ends_at - starts_at) if ends_at else None
            categories = str(component.get("CATEGORIES", "") or "")
            course = _match_course(f"{categories} {summary}", known_courses)
            kind = classify_kind(summary)

            starts = _expand_rrule(component, starts_at, window)
            if not starts:
                errors.append(
                    f"VEVENT '{summary}' recurs but has no occurrence inside the "
                    f"semester window {window[0]}..{window[1]}; nothing imported for it"
                )
                continue
            for s in starts:
                events.append(make_event(
                    "ics", course, summary, s,
                    (s + duration) if duration is not None else None,
                    all_day, kind,
                ))
        except Exception as e:
            errors.append(f"VEVENT '{summary}': {type(e).__name__}: {e}")
    return events, errors


_CSV_COLUMNS = ["course", "title", "date", "start_time", "end_time", "all_day", "kind"]


def parse_fixed_csv(path: Path) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    errors: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        got = [c.strip().lower() for c in (reader.fieldnames or [])]
        if got != _CSV_COLUMNS:
            raise BrainError(
                f"fixed_csv header must be exactly '{','.join(_CSV_COLUMNS)}', got '{','.join(got)}'"
            )
        for lineno, row in enumerate(reader, start=2):
            try:
                course = (row["course"] or "").strip()
                title = (row["title"] or "").strip()
                if not course or not title:
                    raise ValueError("course and title are required")
                d = date.fromisoformat((row["date"] or "").strip())
                all_day = (row["all_day"] or "").strip().lower() in ("true", "1", "yes")
                kind = (row["kind"] or "").strip().lower()
                if kind not in ("exam", "project", "quiz", "admin"):
                    raise ValueError(f"kind '{kind}' not one of exam|project|quiz|admin")
                start_s = (row["start_time"] or "").strip()
                end_s = (row["end_time"] or "").strip()
                if not all_day and not start_s:
                    raise ValueError(
                        "all_day is false but start_time is empty - set a start_time "
                        "or mark the row all_day=true"
                    )
                if all_day:
                    starts_at = datetime.combine(d, time(0, 0))
                    ends_at = None
                else:
                    start_t = _parse_hhmm(start_s)
                    starts_at = datetime.combine(d, start_t)
                    ends_at = None
                    if end_s:
                        end_t = _parse_hhmm(end_s)
                        if end_t <= start_t:
                            raise ValueError(
                                f"end_time {end_s} is not after start_time {start_s}"
                            )
                        ends_at = datetime.combine(d, end_t)
                events.append(make_event("csv", course, title, starts_at, ends_at, all_day, kind))
            except Exception as e:
                errors.append(f"line {lineno}: {e}")
    return events, errors


def _parse_hhmm(s: str) -> time:
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ValueError(f"time '{s}' is not HH:MM")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"time '{s}' is out of range")
    return time(hh, mm)


def expand_recurring(cal: CalendarConfig) -> list[Event]:
    """Expand recurring rules across [semester_start, semester_end], skipping
    any date inside a break range."""
    events: list[Event] = []
    for rule in cal.recurring:
        d = cal.semester_start
        while d <= cal.semester_end:
            if d.weekday() in rule.weekdays and not any(b.contains(d) for b in cal.breaks):
                starts_at = datetime.combine(d, rule.start)
                ends_at = datetime.combine(d, rule.end)
                events.append(make_event(
                    "recurring", rule.course, rule.title, starts_at, ends_at,
                    False, rule.kind,
                ))
            d += timedelta(days=1)
    return events


# ------------------------------------------------------------------ import

def import_calendar(config: Config, conn: Connection) -> CalendarImportReport:
    """Rebuild events from all configured sources. Idempotent.

    A source is DELETEd and rebuilt only when all of its inputs parsed with
    zero errors; otherwise its parsed events are upserted and the previous
    ones are left alone (never destroy data because an input broke).
    """
    if config.calendar is None:
        raise BrainError("No [calendar] section in config.toml - nothing to import.")
    cal = config.calendar
    known_courses = sorted(
        {r.course for r in cal.recurring} | set(config.collection_names()),
        key=len, reverse=True,
    )
    window = (cal.semester_start, cal.semester_end)
    report = CalendarImportReport()
    by_source: dict[str, list[Event]] = {"ics": [], "csv": [], "recurring": []}
    clean: dict[str, bool] = {"ics": True, "csv": True, "recurring": True}

    # ---- subscribed ICS feeds (OAKS/D2L, Google Calendar) --------------
    feed_files: list[tuple[str, Path]] = []
    for url in cal.ics_urls:
        # NOT the raw url. A Google Calendar "secret address in iCal format"
        # is a bearer credential to the whole calendar, and this detail is
        # persisted to data/calendar_status.json and rendered in the UI. The
        # guide asks every friend to paste that link during setup, so storing
        # it verbatim put a live secret in a file the app then invites them
        # to send when something breaks.
        sr = SourceReport(source="ics", detail=feeds.label_url(url))
        report.sources.append(sr)
        result = feeds.fetch(url, config.settings.data_dir)
        if result.error:
            sr.errors.append(
                f"fetch failed: {result.error}"
                + (" - using the last downloaded copy" if result.stale else "")
            )
            clean["ics"] = False
        if result.path is None:
            sr.parsed = False
            continue
        feed_files.append((url, result.path))

    # ---- ICS (local files + whatever the feeds downloaded) -------------
    local = [(str(p), p) for p in cal.ics_paths]
    for label, path in local + feed_files:
        # Feeds already have a SourceReport; local files need one. Feed rows
        # are keyed by the MASKED label, since that is what was stored above.
        want = feeds.label_url(label) if label in cal.ics_urls else label
        sr = next((s for s in report.sources if s.detail == want), None)
        if sr is None:
            sr = SourceReport(source="ics", detail=label)
            report.sources.append(sr)
        if not path.exists():
            sr.errors.append("file does not exist")
            sr.parsed = False
            clean["ics"] = False
            continue
        try:
            events, errors = parse_ics_file(path, known_courses, window)
            sr.imported = len(events)
            sr.errors.extend(errors)
            by_source["ics"].extend(events)
            if errors:
                clean["ics"] = False
        except Exception as e:
            sr.errors.append(f"failed to parse: {type(e).__name__}: {e}")
            sr.parsed = False
            clean["ics"] = False

    # ---- fixed CSV ----------------------------------------------------
    if cal.fixed_csv is not None:
        sr = SourceReport(source="csv", detail=str(cal.fixed_csv))
        report.sources.append(sr)
        if not cal.fixed_csv.exists():
            sr.errors.append("file does not exist")
            sr.parsed = False
            clean["csv"] = False
        else:
            try:
                events, errors = parse_fixed_csv(cal.fixed_csv)
                sr.imported = len(events)
                sr.errors = errors
                by_source["csv"].extend(events)
                if errors:
                    clean["csv"] = False
            except Exception as e:
                sr.errors.append(f"failed to parse: {type(e).__name__}: {e}")
                sr.parsed = False
                clean["csv"] = False

    # ---- recurring rules ----------------------------------------------
    sr = SourceReport(source="recurring", detail="rules")
    report.sources.append(sr)
    events = expand_recurring(cal)
    sr.imported = len(events)
    by_source["recurring"] = events

    # ---- write ---------------------------------------------------------
    for source, source_events in by_source.items():
        # A source with no configured inputs still gets rebuilt, so removing
        # every ics path clears stale ics events instead of stranding them.
        full_rebuild = clean[source]
        report.full_rebuild[source] = full_rebuild
        if full_rebuild:
            conn.execute("DELETE FROM events WHERE source = ?", (source,))
        written: set[str] = set()
        for ev in source_events:
            conn.execute(
                "INSERT OR REPLACE INTO events "
                "(id, course, title, starts_at, ends_at, all_day, kind, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev.id, ev.course, ev.title, ev.starts_at.isoformat(),
                    ev.ends_at.isoformat() if ev.ends_at else None,
                    int(ev.all_day), ev.kind, ev.source,
                ),
            )
            written.add(ev.id)

        # Distinct ids actually written, so no report claims more events than
        # the database holds when two entries hash identically.
        reports = [s for s in report.sources if s.source == source]
        parsed_total = sum(s.imported for s in reports)
        collapsed = parsed_total - len(written)
        for s in reports:
            s.stored = s.imported
        if collapsed > 0 and reports:
            reports[0].stored = reports[0].imported - collapsed
            reports[0].errors.append(
                f"{collapsed} event(s) share a course, title and start time with "
                f"another entry and collapsed into it; {len(written)} distinct "
                f"events stored for source '{source}'"
            )
    conn.commit()
    return report


# ------------------------------------------------------------------ queries

def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "course": row["course"],
        "title": row["title"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "all_day": bool(row["all_day"]),
        "kind": row["kind"],
        "source": row["source"],
    }


def events_between(conn: Connection, start: datetime, end: datetime) -> list[dict]:
    """Events overlapping [start, end): an event counts if it starts inside
    the window, or if it started earlier and runs into it (multi-day events)."""
    rows = conn.execute(
        "SELECT * FROM events "
        "WHERE starts_at < ? AND COALESCE(ends_at, starts_at) >= ? "
        "ORDER BY starts_at",
        (end.isoformat(), start.isoformat()),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _deadline_cutoff_sql(column_alias: str = "") -> str:
    """SQL expression for when a deadline stops being 'upcoming'. All-day
    events are due at the end of their day, not at 00:00, so they stay in
    Next up and the due-soon count for the whole day they fall on."""
    p = f"{column_alias}." if column_alias else ""
    return (
        f"(CASE WHEN {p}all_day = 1 "
        f"THEN substr({p}starts_at, 1, 10) || 'T23:59:59' "
        f"ELSE {p}starts_at END)"
    )


def next_events(
    conn: Connection, now: datetime, *, limit: int = 8,
    kinds: tuple[str, ...] | None = DEADLINE_KINDS,
    collapse_repeats: bool = False,
) -> list[dict]:
    """Upcoming events, deadline kinds by default (class meetings excluded).
    All-day events remain 'upcoming' until the end of their day.

    collapse_repeats keeps only the soonest instance of each recurring piece
    of work (same course + title). Homework due before every class is real,
    but listing the next eight of them tells you nothing you did not already
    know and hides the exam behind them. Counts and the calendar still see
    every instance - this only thins the "what's next" list.
    """
    cutoff = _deadline_cutoff_sql()
    q = f"SELECT * FROM events WHERE {cutoff} >= ?"
    params: list = [now.isoformat()]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    q += " ORDER BY starts_at"
    if limit is not None and not collapse_repeats:
        q += " LIMIT ?"
        params.append(limit)

    rows = [_row_to_dict(r) for r in conn.execute(q, params)]
    if not collapse_repeats:
        return rows

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["course"], r["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if limit is not None and len(out) >= limit:
            break
    return out


def due_within(conn: Connection, now: datetime, days: int) -> int:
    end = now + timedelta(days=days)
    cutoff = _deadline_cutoff_sql()
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM events WHERE {cutoff} >= ? AND starts_at < ? "
        f"AND kind IN ({','.join('?' * len(DEADLINE_KINDS))})",
        (now.isoformat(), end.isoformat(), *DEADLINE_KINDS),
    ).fetchone()
    return row["n"]


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_load(conn: Connection, config: Config) -> list[dict]:
    """Events per Monday-started week, excluding kind=admin (spec). The week
    range spans the semester AND every event outside it, in both directions,
    so nothing is silently dropped from the heat row."""
    if config.calendar is None:
        return []
    rows = conn.execute(
        "SELECT starts_at, kind FROM events WHERE kind != 'admin'"
    ).fetchall()
    counts: dict[date, int] = {}
    deadline_counts: dict[date, int] = {}
    for r in rows:
        d = monday_of(datetime.fromisoformat(r["starts_at"]).date())
        counts[d] = counts.get(d, 0) + 1
        # Deadline-only tally rides along for the calendar's semester ruler:
        # class meetings dominate the raw count (5 courses x ~3 meetings/wk)
        # and flatten the crunch weeks a student actually needs to see.
        if r["kind"] != "recurring":
            deadline_counts[d] = deadline_counts.get(d, 0) + 1

    semester_start = monday_of(config.calendar.semester_start)
    semester_end = monday_of(config.calendar.semester_end)
    start = min([semester_start, *counts.keys()]) if counts else semester_start
    end = max([semester_end, *counts.keys()]) if counts else semester_end
    weeks: list[dict] = []
    w = start
    while w <= end:
        weeks.append({"week_start": w.isoformat(), "count": counts.get(w, 0),
                      "deadlines": deadline_counts.get(w, 0)})
        w += timedelta(days=7)
    return weeks
