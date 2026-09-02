"""Aggregates for the dashboard. Pure reads over the events and chunks tables.

Every figure here answers a question a person actually asks: how loaded is the
rest of my semester, which course is about to bury me, what does the index
actually contain, and how far through the term am I.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from sqlite3 import Connection

from .calendar import DEADLINE_KINDS, monday_of, week_load
from .config import Config


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def semester_progress(config: Config, today: date) -> dict:
    """How far through the term, in days and percent."""
    if config.calendar is None:
        return {}
    start, end = config.calendar.semester_start, config.calendar.semester_end
    span = (end - start).days or 1
    elapsed = (today - start).days
    pct = max(0.0, min(1.0, elapsed / span))
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_days": span,
        "elapsed_days": max(0, elapsed),
        "days_remaining": max(0, (end - today).days),
        "weeks_remaining": max(0, (end - today).days // 7),
        "pct_elapsed": round(pct * 100, 1),
    }


def by_course(conn: Connection, config: Config, now: datetime) -> list[dict]:
    """Per-course deadline counts, split by kind, with the next one due.

    Counts only what is still ahead - a dashboard is about what is coming, and
    a course whose work is done should read as done.
    """
    colors = {c.name.upper(): c.color for c in config.collections}
    rows = conn.execute(
        f"SELECT course, title, kind, starts_at, all_day FROM events "
        f"WHERE kind IN ({','.join('?' * len(DEADLINE_KINDS))}) "
        f"ORDER BY starts_at",
        DEADLINE_KINDS,
    ).fetchall()

    def blank(course: str) -> dict:
        return {
            "course": course,
            "color": colors.get(course.upper(), "#8b93a3"),
            "total": 0, "remaining": 0,
            "exam": 0, "quiz": 0, "project": 0,
            "next_title": None, "next_at": None, "next_kind": None,
            "days_until_next": None, "note": "",
        }

    agg: dict[str, dict] = {}
    # Seed every course that meets this semester, so a course with no
    # published due dates shows up as "none scheduled" rather than vanishing
    # from a table titled "by course".
    if config.calendar is not None:
        for rule in config.calendar.recurring:
            agg.setdefault(rule.course, blank(rule.course))

    for r in rows:
        course = r["course"]
        bucket = agg.setdefault(course, blank(course))
        bucket["total"] += 1
        starts = _parse(r["starts_at"])
        if starts >= now:
            bucket["remaining"] += 1
            bucket[r["kind"]] += 1
            if bucket["next_at"] is None:
                bucket["next_title"] = r["title"]
                bucket["next_at"] = r["starts_at"]
                bucket["next_kind"] = r["kind"]
                bucket["days_until_next"] = (starts.date() - now.date()).days
    out = list(agg.values())
    for b in out:
        if b["total"] == 0:
            b["note"] = "no dated work published"
        elif b["remaining"] == 0:
            b["note"] = "all deadlines passed"
    # Most-loaded first; a course with nothing left sinks to the bottom.
    out.sort(key=lambda b: (-b["remaining"], b["course"]))
    return out


def by_kind(conn: Connection, now: datetime) -> dict:
    rows = conn.execute(
        f"SELECT kind, COUNT(*) AS n FROM events "
        f"WHERE kind IN ({','.join('?' * len(DEADLINE_KINDS))}) AND starts_at >= ? "
        f"GROUP BY kind",
        (*DEADLINE_KINDS, now.isoformat()),
    ).fetchall()
    counts = {k: 0 for k in DEADLINE_KINDS}
    for r in rows:
        counts[r["kind"]] = r["n"]
    return counts


def daily_load(conn: Connection, config: Config, now: datetime, days: int = 28) -> list[dict]:
    """Deadline count per day for the next `days`, for the density strip."""
    end = now + timedelta(days=days)
    rows = conn.execute(
        f"SELECT course, starts_at FROM events "
        f"WHERE kind IN ({','.join('?' * len(DEADLINE_KINDS))}) "
        f"AND starts_at >= ? AND starts_at < ?",
        (*DEADLINE_KINDS, now.isoformat(), end.isoformat()),
    ).fetchall()
    per_day: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        per_day[r["starts_at"][:10]][r["course"]] += 1

    out = []
    for i in range(days):
        d = (now.date() + timedelta(days=i)).isoformat()
        courses = per_day.get(d, Counter())
        out.append({
            "date": d,
            "count": sum(courses.values()),
            "courses": dict(courses),
        })
    return out


def week_load_by_course(conn: Connection, config: Config) -> dict:
    """Week-load split by course, so the semester bar chart can stack.

    Uses the same rule as the plain week-load (everything except admin), so
    the two views can never disagree.
    """
    rows = conn.execute(
        "SELECT course, starts_at, kind FROM events WHERE kind != 'admin'"
    ).fetchall()
    weeks: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        w = monday_of(_parse(r["starts_at"]).date()).isoformat()
        weeks[w][r["course"]] += 1

    base = week_load(conn, config)
    courses = sorted({c for w in weeks.values() for c in w})
    return {
        "weeks": [
            {"week_start": w["week_start"], "count": w["count"],
             "by_course": dict(weeks.get(w["week_start"], {}))}
            for w in base
        ],
        "courses": courses,
    }


def busiest_days(conn: Connection, now: datetime, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        f"SELECT starts_at, title, course FROM events "
        f"WHERE kind IN ({','.join('?' * len(DEADLINE_KINDS))}) AND starts_at >= ?",
        (*DEADLINE_KINDS, now.isoformat()),
    ).fetchall()
    per_day: dict[str, list] = defaultdict(list)
    for r in rows:
        per_day[r["starts_at"][:10]].append({"title": r["title"], "course": r["course"]})
    ranked = sorted(per_day.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [
        {"date": d, "count": len(items), "items": items}
        for d, items in ranked[:limit] if len(items) > 1
    ]


def index_composition(conn: Connection, config: Config) -> list[dict]:
    """What the searchable index is actually made of, by collection."""
    colors = {c.name: c.color for c in config.collections}
    levels = {c.name: c.assist_level for c in config.collections}
    rows = conn.execute(
        "SELECT collection, COUNT(*) AS chunks, COUNT(DISTINCT source_path) AS docs "
        "FROM chunks GROUP BY collection"
    ).fetchall()
    total = sum(r["chunks"] for r in rows) or 1
    out = [
        {
            "collection": r["collection"],
            "color": colors.get(r["collection"], "#8b93a3"),
            "assist_level": levels.get(r["collection"], "full"),
            "docs": r["docs"],
            "chunks": r["chunks"],
            "share": round(r["chunks"] / total * 100, 1),
        }
        for r in rows
    ]
    # Configured but never indexed - shown so an empty collection is visible.
    seen = {o["collection"] for o in out}
    for c in config.collections:
        if c.name not in seen:
            out.append({
                "collection": c.name, "color": c.color,
                "assist_level": c.assist_level,
                "docs": 0, "chunks": 0, "share": 0.0,
            })
    out.sort(key=lambda o: -o["chunks"])
    return out


def file_types(conn: Connection) -> list[dict]:
    """Chunk counts by file extension - what the index is built from."""
    rows = conn.execute("SELECT source_path FROM chunks").fetchall()
    counts = Counter()
    for r in rows:
        ext = r["source_path"].rsplit(".", 1)[-1].lower() if "." in r["source_path"] else "(none)"
        counts[ext] += 1
    total = sum(counts.values()) or 1
    return [
        {"ext": ext, "chunks": n, "share": round(n / total * 100, 1)}
        for ext, n in counts.most_common()
    ]


def build(conn: Connection, config: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    courses = by_course(conn, config, now)
    composition = index_composition(conn, config)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "semester": semester_progress(config, now.date()),
        "by_course": courses,
        "by_kind": by_kind(conn, now),
        "daily_load": daily_load(conn, config, now),
        "week_load": week_load_by_course(conn, config),
        "busiest_days": busiest_days(conn, now),
        "index": composition,
        "file_types": file_types(conn),
        "totals": {
            "deadlines_remaining": sum(c["remaining"] for c in courses),
            "exams_remaining": sum(c["exam"] for c in courses),
            "quizzes_remaining": sum(c["quiz"] for c in courses),
            "projects_remaining": sum(c["project"] for c in courses),
            "docs": sum(c["docs"] for c in composition),
            "chunks": sum(c["chunks"] for c in composition),
            "collections_indexed": sum(1 for c in composition if c["chunks"] > 0),
            "collections_total": len(composition),
        },
    }
