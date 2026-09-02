"""The four platform connectors.

Each declares how to reach its own JSON endpoints. Because these APIs are
session- and version-specific, the per-site response PARSER is finalized
against a real captured response (see `brain sync capture`) rather than
guessed - a wrong parser that silently returns nothing is worse than an
honest "not wired yet". The framework around them (session replay, login
detection, change reconciliation, calendar merge) is complete and tested.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from .base import Connector, LoginRequired, PulledItem, ensure_where
from .detect import _key as _match_key


class _CaptureFirst(Connector):
    """A connector whose endpoints are known but whose parser needs one real
    captured response to finalize. pull() fetches + hands off to parse()."""
    endpoints: list[str] = []

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        from . import http
        items: list[PulledItem] = []
        for url in self.endpoints:
            data = http.get_json(session, url.format(base=session.get("base_url", "")), self.name)
            items.extend(self.parse(data, courses))
        if not items:
            raise LoginRequired(
                f"{self.name}: reached the site but no assignments were parsed. "
                f"The response shape needs to be captured once so the parser can "
                f"be finalized:\n    brain sync capture {self.name} <a URL from the "
                f"site's network tab that returns your assignments as JSON>"
            )
        return items

    def parse(self, data, courses: list[str]) -> list[PulledItem]:  # noqa: ARG002
        return []


def _current_term(today: date | None = None) -> str:
    """The term string as it appears in D2L OrgUnitNames, e.g. '2026 Fall'."""
    d = today or date.today()
    if d.month >= 8:
        season = "Fall"
    elif d.month <= 5:
        season = "Spring"
    else:
        season = "Summer"
    return f"{d.year} {season}"


# "(FINC-315-01)" or "(SPAN-200-04-05-07)" inside an OrgUnitName.
_COURSE_CODE = re.compile(r"\(([A-Za-z]{2,5})-(\d{3})")

_EXAM = re.compile(r"\b(exam|examen|final|midterm)\b", re.IGNORECASE)
_PROJECT = re.compile(r"\b(project|paper|presentation|proyecto|talk abroad)\b",
                      re.IGNORECASE)
_QUIZ = re.compile(r"\b(quiz|prueba|test)\b", re.IGNORECASE)
_ADMIN = re.compile(r"\b(office hours?|review session|orientation|syllabus|"
                    r"drop.?in|zoom link|no class|lecture|sample questions?|"
                    r"gu[ií]a de estudio|study guide)\b",
                    re.IGNORECASE)
# "Week 3 - Chapter 4 ..." course-shell content modules: reading-schedule
# targets, not graded deadlines - keep them, but as admin (like the Blended
# pacing rows), so they never count toward week load or look like a quiz.
_WEEK_TOPIC = re.compile(r"^\s*week\s*\d", re.IGNORECASE)


# Phrases that genuinely mean "not graded work", as opposed to words that
# merely CO-OCCUR with it. "Quiz 3 study guide" is admin; "Chapter 4 Quiz (see
# syllabus)" is a quiz that happens to mention the syllabus. Splitting the old
# single _ADMIN pattern is what stops a real deadline being classified as
# decoration and vanishing from every deadline view.
_ADMIN_STRONG = re.compile(
    r"\b(office hours?|review session|orientation|drop.?in|zoom link|"
    r"no class|sample questions?|gu[ií]a de estudio|study guide)\b",
    re.IGNORECASE)
# Weak signals: enough to classify a title that says nothing else, but they
# must NOT outrank the word "quiz" or "exam" in the same title.
_ADMIN_WEAK = re.compile(r"\b(syllabus|lecture)\b", re.IGNORECASE)


def _classify(title: str) -> str:
    # Unambiguous admin phrasing wins outright: "Final Exam Review Session"
    # is a session, not an exam.
    if _ADMIN_STRONG.search(title):
        return "admin"
    # A LEADING "Week N" is a content-module title from the course shell
    # ("Week 12: Midterm and Chapter 18 Pension Plans" lists what that week
    # covers; it is not the midterm). That prefix outranks the graded words,
    # which is why it is tested before them.
    if _WEEK_TOPIC.match(title):
        return "admin"
    # Graded work now beats the WEAK words. "syllabus" or "lecture" appearing
    # anywhere used to demote a genuine graded item to admin, which removes it
    # from Next up, the due-soon count, the workload chart and the Today plan
    # - a deadline the student is never shown.
    if _PROJECT.search(title):
        return "project"
    if _EXAM.search(title):
        return "exam"
    if _QUIZ.search(title):
        return "quiz"
    if _ADMIN_WEAK.search(title):
        return "admin"
    return "quiz"   # dated coursework defaults to a deadline, not decoration


class OaksConnector(Connector):
    """OAKS (D2L Brightspace) via its official REST API, on the user's own
    browser session.

    Two requests shapes, both verified against the live site 2026-08-25:
      1. lp myenrollments -> map current-term course offerings to the
         configured collection names (FINC-315-01 -> FINC315). Nothing is
         hardcoded per semester.
      2. le {orgUnitId}/calendar/events/ per course -> every dated item the
         instructors published. The route ignores date-window query params and
         returns years of reused-shell history, so the window filter here is
         load-bearing, not defensive.
    Times arrive in UTC; the calendar stores local (America/New_York) dates,
    so conversion happens here, once.
    """

    name = "oaks"
    label = "OAKS / D2L Brightspace"
    login_hint = (
        "Log in to https://lms.cofc.edu in your browser, then store the "
        "session: DevTools > Network > right-click the top request > Copy > "
        "'Copy request headers', and paste into: brain sync login oaks"
    )
    base = "https://lms.cofc.edu"
    LP = "1.31"   # enrollments API version (verified supported)
    LE = "1.96"   # calendar API version (latest advertised by the server)

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        from . import http

        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        if not targets:
            raise LoginRequired(
                f"{self.name}: the session works but no current-term course "
                f"offering matched the configured collections {courses!r}. "
                f"Check that collection names match the course codes."
            )
        items: list[PulledItem] = []
        for ouid, course in sorted(targets.items()):
            events = http.get_json(
                session,
                f"{self.base}/d2l/api/le/{self.LE}/{ouid}/calendar/events/",
                self.name,
            )
            ev_items = self.parse_events(events, course, window_hi=window_hi)
            # quizzes/ is the AUTHORITATIVE quiz record: its DueDate is the
            # real deadline, while calendar/events serves 2-3 identically
            # titled events per quiz (availability-open/due/close) whose
            # first-in-order timestamp is usually the OPEN time. Live
            # observed 2026-08-31: a quiz due moved 16:00Z -> 14:00Z and the
            # events feed alone left the calendar two hours late on it.
            # Per-course isolation: the quiz tool can be disabled or
            # restricted on one course, and that must degrade to the
            # events-only behavior for that course rather than failing the
            # whole site (which would read as "logged out" on the dashboard).
            try:
                quiz_items = self.parse_quizzes(
                    self._fetch_quizzes(session, ouid), course,
                    window_hi=window_hi)
            except Exception:
                quiz_items = []
            items.extend(self._merge_quiz_authority(ev_items, quiz_items))
            # Dropbox folders are submissions that live ON OAKS - the one
            # source where "where do I do this" is knowable with certainty.
            # (FINC389's Initial Stock Portfolio was a dropbox with a due
            # date that appeared in no calendar.)
            dropbox = http.get_json(
                session,
                f"{self.base}/d2l/api/le/{self.LE}/{ouid}/dropbox/folders/",
                self.name,
            )
            items.extend(self.parse_dropbox(dropbox, course, window_hi=window_hi))
        return items

    def _fetch_quizzes(self, session: dict, ouid: int) -> list:
        """All quiz objects for one course, following D2L's paged objectlist
        (Objects + Next href). Live courses fit one page; the loop is a
        guard, not an expectation."""
        from . import http

        objects: list = []
        url = f"{self.base}/d2l/api/le/{self.LE}/{ouid}/quizzes/"
        for _ in range(10):
            data = http.get_json(session, url, self.name)
            if isinstance(data, list):          # defensive: bare list
                objects.extend(data)
                break
            objects.extend(data.get("Objects") or [])
            nxt = data.get("Next")
            if not nxt:
                break
            url = nxt if str(nxt).startswith("http") else f"{self.base}{nxt}"
        return objects

    def map_enrollments(self, enr: dict, courses: list[str],
                        today: date | None = None) -> dict[int, str]:
        """OrgUnitId -> collection name, current term only."""
        wanted = {re.sub(r"[^A-Z0-9]", "", c.upper()): c for c in courses}
        term = _current_term(today)
        out: dict[int, str] = {}
        for it in enr.get("Items", []):
            ou = it.get("OrgUnit") or {}
            if (ou.get("Type") or {}).get("Code") != "Course Offering":
                continue
            name = ou.get("Name") or ""
            if term not in name:
                continue
            m = _COURSE_CODE.search(name)
            if not m:
                continue
            code = (m.group(1) + m.group(2)).upper()
            if code in wanted:
                out[int(ou["Id"])] = wanted[code]
        return out

    def parse_events(self, events: list, course: str,
                     today: date | None = None,
                     window_hi: date | None = None) -> list[PulledItem]:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        lo, hi = self._window(today, window_hi)
        out: list[PulledItem] = []
        for e in events or []:
            title = (e.get("Title") or "").strip()
            # Respondus integration prefixes exam events with "Proctoring
            # Enabled:"; the calendar names the exam itself, so the prefix
            # only defeats matching.
            title = re.sub(r"^\s*proctoring enabled:\s*", "", title,
                           flags=re.IGNORECASE)
            raw = e.get("StartDateTime")
            if not title or not raw:
                continue
            try:
                start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            local = start.astimezone(tz)
            if not (lo <= local.date() <= hi):
                continue   # reused-shell history from prior years
            all_day = bool(e.get("IsAllDayEvent"))
            end_raw = e.get("EndDateTime")
            end_time = ""
            if end_raw and end_raw != raw and not all_day:
                try:
                    end_local = datetime.fromisoformat(
                        end_raw.replace("Z", "+00:00")).astimezone(tz)
                    if end_local.date() == local.date():
                        end_time = end_local.strftime("%H:%M")
                except ValueError:
                    pass
            kind = _classify(title)
            # A recurring admin event is a class meeting or office hours -
            # config.toml's recurring rules already model those, and pulling
            # each occurrence creates phantom "moved" reports forever.
            if kind == "admin" and e.get("IsRecurring"):
                continue
            out.append(PulledItem(
                course=course,
                title=ensure_where(title, "details on OAKS"),
                date=local.date().isoformat(),
                start_time="" if all_day else local.strftime("%H:%M"),
                end_time=end_time,
                all_day=all_day,
                kind=kind,
                site=self.name,
                external_id=str(e.get("CalendarEventId") or ""),
                url=e.get("CalendarEventViewUrl") or "",
            ))
        return out

    # ---- quizzes (authoritative due times + body text) -------------

    @staticmethod
    def _quiz_local(raw, tz):
        """One D2L UTC timestamp -> aware local datetime, or None."""
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)

    @staticmethod
    def _richtext(block) -> str:
        """Text out of a D2L RichTextInput ({Text:{Text,Html},IsDisplayed}
        or bare {Text,Html}), tolerant of either nesting."""
        if not isinstance(block, dict):
            return ""
        inner = block.get("Text")
        if isinstance(inner, dict):
            return (inner.get("Text") or "").strip()
        return (inner or "").strip()

    def parse_quizzes(self, objects: list, course: str,
                      today: date | None = None,
                      window_hi: date | None = None) -> list[PulledItem]:
        """Quiz records -> deadline items, keyed on DueDate.

        A quiz with NO DueDate is deliberately not emitted: its availability
        close can sit weeks after the real in-class date, and the calendar
        event for it (which does carry that date) is the better source. The
        quiz record is authoritative only where it actually states a due
        time. Every timestamp/date the record carries (open/due/close) rides
        along in known_times/known_dates so the merge and reconcile can tell
        this quiz's own availability window from a separate occurrence."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        lo, hi = self._window(today, window_hi)
        out: list[PulledItem] = []
        for q in objects or []:
            name = (q.get("Name") or "").strip()
            name = re.sub(r"^\s*proctoring enabled:\s*", "", name,
                          flags=re.IGNORECASE)
            due = self._quiz_local(q.get("DueDate"), tz)
            if not name or due is None:
                continue
            if not (lo <= due.date() <= hi):
                continue
            times, dates = set(), set()
            for f in ("StartDate", "DueDate", "EndDate"):
                local = self._quiz_local(q.get(f), tz)
                if local is not None:
                    times.add(local.strftime("%H:%M"))
                    dates.add(local.date().isoformat())
            out.append(PulledItem(
                course=course,
                title=ensure_where(name, "on OAKS"),
                date=due.date().isoformat(),
                start_time=due.strftime("%H:%M"),
                kind=_classify(name),
                site=self.name,
                external_id=f"quiz-{q.get('QuizId') or ''}",
                known_times=tuple(sorted(times)),
                known_dates=tuple(sorted(dates)),
            ))
        return out

    @staticmethod
    def _merge_quiz_authority(event_items: list[PulledItem],
                              quiz_items: list[PulledItem]) -> list[PulledItem]:
        """Fold calendar events into the quiz records they duplicate.

        A quiz shows up in calendar/events as 2-3 identically titled events
        (availability-open/due/close, sometimes on different dates). An event
        matching a quiz record by key AND falling inside that quiz's own
        open/due/close dates is dropped - its time only feeds the quiz item's
        known_times - so the open event can never become its own deadline and
        first-in-response-order never wins over the real DueDate.

        The date test is load-bearing: a repeating title whose later
        occurrences are still drip-locked ("Prueba de vocabulario" on 9/4 and
        9/18, with only 9/4 released to the quiz API) must keep the events
        the API cannot see, or the merge would silently delete a real
        deadline. Remaining same-title/same-date event duplicates (non-quiz
        tools) collapse to the latest time, which for D2L's open/due/close
        pattern is the deadline."""
        by_quiz: dict[str, list[PulledItem]] = {}
        for qi in quiz_items:
            by_quiz.setdefault(_match_key(qi.course, qi.title), []).append(qi)
        groups: dict[tuple, PulledItem] = {}
        for ev in event_items:
            k = _match_key(ev.course, ev.title)
            owner = next(
                (qi for qi in by_quiz.get(k, [])
                 if ev.date == qi.date or ev.date in qi.known_dates), None)
            if owner is not None:
                if ev.start_time:
                    owner.known_times = tuple(
                        sorted({*owner.known_times, ev.start_time}))
                continue
            gk = (ev.course, k, ev.date)
            cur = groups.get(gk)
            if cur is None:
                groups[gk] = ev
                continue
            # Union BOTH sides' accumulated times: whichever event wins the
            # comparison must not drop the times already gathered by the
            # other, or a third duplicate would push the earliest (open)
            # time out of known_times and fire a phantom retime.
            times = {t for t in (cur.start_time, ev.start_time,
                                 *cur.known_times, *ev.known_times) if t}
            keep = ev if (ev.start_time or "") > (cur.start_time or "") else cur
            keep.known_times = tuple(sorted(times))
            groups[gk] = keep
        return list(groups.values()) + quiz_items

    def list_quiz_content(self, session: dict, courses: list[str]) -> list[dict]:
        """Per quiz: name, due, attempts, and any body text the instructor
        wrote (Description/Instructions/Header/Footer). This is where a
        posted study guide or format note would live - nothing else exposes
        it. Most quizzes carry no text; the writer skips those."""
        from zoneinfo import ZoneInfo

        from . import http

        tz = ZoneInfo("America/New_York")
        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        out: list[dict] = []
        for ouid, course in sorted(targets.items()):
            try:
                objects = self._fetch_quizzes(session, ouid)
            except Exception as e:
                out.append({"course": course, "error": f"{type(e).__name__}: {e}"})
                continue
            for q in objects:
                name = (q.get("Name") or "").strip()
                if not name:
                    continue
                due = self._quiz_local(q.get("DueDate") or q.get("EndDate"), tz)
                att = q.get("AttemptsAllowed") or {}
                if att.get("IsUnlimited"):
                    attempts = "unlimited attempts"
                elif att.get("NumberOfAttemptsAllowed"):
                    n = att["NumberOfAttemptsAllowed"]
                    attempts = f"{n} attempt" + ("s" if n != 1 else "")
                else:
                    attempts = ""
                out.append({
                    "course": course,
                    "id": f"oaks-quiz-{q.get('QuizId') or ''}",
                    "name": name,
                    "due": due.strftime("%Y-%m-%d %H:%M") if due else "",
                    "attempts": attempts,
                    "description": self._richtext(q.get("Description")),
                    "instructions": self._richtext(q.get("Instructions")),
                    "header": self._richtext(q.get("Header")),
                    "footer": self._richtext(q.get("Footer")),
                })
        return out

    # ---- grades ----------------------------------------------------

    def list_grades(self, session: dict, courses: list[str]) -> list[dict]:
        """Per course: the gradebook structure plus the user's own values.

        Structure comes from /grades/ (every item with MaxPoints/Weight,
        present from day one); values from /grades/values/myGradeValues/
        (empty list until things are graded - week 2 verified live).
        """
        from . import http

        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        out: list[dict] = []
        for ouid, course in sorted(targets.items()):
            # Per-course isolation: a transient failure on course 3 of 5 must
            # not abort the whole site's pull (get_json converts any network
            # hiccup into LoginRequired, which would otherwise look like a
            # dead session). The failed course reports its error and the
            # rest still refresh.
            try:
                objects = http.get_json(
                    session, f"{self.base}/d2l/api/le/{self.LE}/{ouid}/grades/",
                    self.name,
                )
                values = http.get_json(
                    session,
                    f"{self.base}/d2l/api/le/{self.LE}/{ouid}/grades/values/myGradeValues/",
                    self.name,
                )
            except Exception as e:
                out.append({"course": course, "ou": ouid, "items": [],
                            "error": f"{type(e).__name__}: {e}"})
                continue
            out.append(self.parse_grades(objects, values, course, ouid))
        return out

    def parse_grades(self, objects: list, values: list, course: str,
                     ouid: int) -> dict:
        by_id = {}
        for v in values or []:
            gid = v.get("GradeObjectIdentifier")
            if gid is not None:
                by_id[str(gid)] = v
        # D2L aggregate grade objects (Category=5, FinalCalculated=7,
        # FinalAdjusted=8, Formula=9) roll OTHER items up; keeping them
        # would double-count every point they summarize. Absent/unknown
        # types stay, so nothing real silently disappears.
        AGGREGATE_TYPES = {5, 7, 8, 9}
        items = []
        for g in objects or []:
            if g.get("IsHidden"):
                continue
            if g.get("GradeObjectType") in AGGREGATE_TYPES:
                continue
            v = by_id.get(str(g.get("Id")), {})
            num, den = v.get("PointsNumerator"), v.get("PointsDenominator")
            items.append({
                "name": (g.get("Name") or "").strip(),
                "max_points": g.get("MaxPoints"),
                "weight": g.get("Weight"),
                "bonus": bool(g.get("IsBonus")),
                "excluded": bool(g.get("ExcludeFromFinalGradeCalculation")),
                "graded": num is not None,
                "score": num,
                "out_of": den if den is not None else g.get("MaxPoints"),
                "displayed": v.get("DisplayedGrade") or "",
                "weighted_num": v.get("WeightedNumerator"),
                "weighted_den": v.get("WeightedDenominator"),
            })
        return {"course": course, "ou": ouid, "items": items}

    # ---- announcements (news) --------------------------------------

    def list_news(self, session: dict, courses: list[str]) -> list[dict]:
        """Course announcements, newest first: {course, id, title, date,
        text}. Instructors post reading assignments and schedule changes
        here (verified live: FINC315 posts WSJ/NYT readings as news)."""
        from . import http

        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        out: list[dict] = []
        for ouid, course in sorted(targets.items()):
            news = http.get_json(
                session, f"{self.base}/d2l/api/le/{self.LE}/{ouid}/news/",
                self.name,
            )
            out.extend(self.parse_news(news, course))
        out.sort(key=lambda n: n["date"], reverse=True)
        return out

    def parse_news(self, news: list, course: str) -> list[dict]:
        out: list[dict] = []
        for n in news or []:
            if n.get("IsHidden") or not n.get("IsPublished", True):
                continue
            body = n.get("Body") or {}
            text = (body.get("Text") or "").strip()
            out.append({
                "course": course,
                "id": f"oaks-{n.get('Id')}",
                "title": (n.get("Title") or "").strip(),
                "date": (n.get("StartDate") or n.get("CreatedDate") or "")[:10],
                "text": text,
            })
        return out

    # ---- course files (content ToC) --------------------------------

    def list_files(self, session: dict, courses: list[str]) -> list[dict]:
        """Every File topic in each current course's content, flattened:
        {course, topic_id, ou, title, filename, module_path}. Used by
        `brain sync files` to pull newly-uploaded course materials."""
        from . import http

        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        out: list[dict] = []
        for ouid, course in sorted(targets.items()):
            toc = http.get_json(
                session,
                f"{self.base}/d2l/api/le/{self.LE}/{ouid}/content/toc",
                self.name,
            )
            out.extend(self.walk_toc(toc, course, ouid))
        return out

    def list_links(self, session: dict, courses: list[str]) -> list[dict]:
        """Every 'Link' content topic across current courses: {course, title,
        url, module_path}. These are Google Docs, SharePoint files, articles,
        etc. that the resolver turns into searchable notes."""
        from . import http

        enr = http.get_json(
            session,
            f"{self.base}/d2l/api/lp/{self.LP}/enrollments/myenrollments/?isActive=true",
            self.name,
        )
        targets = self.map_enrollments(enr, courses)
        out: list[dict] = []
        for ouid, course in sorted(targets.items()):
            toc = http.get_json(
                session, f"{self.base}/d2l/api/le/{self.LE}/{ouid}/content/toc",
                self.name,
            )
            out.extend(self._walk_toc_kind(toc, course, "Link"))
        return out

    def _walk_toc_kind(self, toc: dict, course: str, want: str) -> list[dict]:
        out: list[dict] = []

        def walk(module: dict, path: list[str]) -> None:
            here = path + [module.get("Title") or ""]
            for t in module.get("Topics") or []:
                if t.get("TypeIdentifier") != want:
                    continue
                out.append({
                    "course": course,
                    "title": (t.get("Title") or "").strip(),
                    "url": t.get("Url") or "",
                    "module_path": " > ".join(p for p in here if p),
                })
            for sub in module.get("Modules") or []:
                walk(sub, here)

        for m in toc.get("Modules") or []:
            walk(m, [])
        return out

    def walk_toc(self, toc: dict, course: str, ouid: int) -> list[dict]:
        out: list[dict] = []

        def walk(module: dict, path: list[str]) -> None:
            here = path + [module.get("Title") or ""]
            for t in module.get("Topics") or []:
                if t.get("TypeIdentifier") != "File":
                    continue
                url = t.get("Url") or ""
                filename = url.rsplit("/", 1)[-1] if "/" in url else ""
                # Windows-hostile characters out of the filename
                filename = re.sub(r'[<>:"|?*\\]', "_", filename).strip()
                if not filename:
                    continue
                out.append({
                    "course": course,
                    "ou": ouid,
                    "topic_id": t.get("Identifier"),
                    "title": (t.get("Title") or "").strip(),
                    "filename": filename,
                    "module_path": " > ".join(p for p in here if p),
                })
            for sub in module.get("Modules") or []:
                walk(sub, here)

        for m in toc.get("Modules") or []:
            walk(m, [])
        return out

    def download_file(self, session: dict, ou: int, topic_id, dest) -> int:
        """Stream one content topic's file to dest; returns bytes written.

        Course files run to several MB (slide decks, spreadsheets), so this
        uses a generous timeout rather than the short API-call default that
        was truncating large downloads."""
        from . import http as http_mod

        c = http_mod.client(session)
        c.timeout = __import__("httpx").Timeout(120.0)
        with c:
            resp = c.get(
                f"{self.base}/d2l/api/le/{self.LE}/{ou}/content/topics/{topic_id}/file")
            if resp.status_code != 200:
                raise LoginRequired(
                    f"{self.name}: download failed (HTTP {resp.status_code}) "
                    f"for topic {topic_id}")
            data = resp.content
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return len(data)

    def parse_dropbox(self, folders: list, course: str,
                      today: date | None = None,
                      window_hi: date | None = None) -> list[PulledItem]:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        lo, hi = self._window(today, window_hi)
        out: list[PulledItem] = []
        for f in folders or []:
            name = (f.get("Name") or "").strip()
            due = f.get("DueDate")
            if not name or not due:
                continue   # undated dropboxes are not deadlines yet
            try:
                local = datetime.fromisoformat(
                    due.replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                continue
            if not (lo <= local.date() <= hi):
                continue
            out.append(PulledItem(
                course=course,
                title=ensure_where(name, "submit on OAKS"),
                date=local.date().isoformat(),
                start_time=local.strftime("%H:%M"),
                kind="project",
                site=self.name,
                external_id=f"dropbox-{f.get('Id') or ''}",
            ))
        return out


class ConnectConnector(Connector):
    """McGraw Hill Connect (newconnect student app).

    Auth is two-layer: the long-lived ERIGHTS cookie mints a 5-minute
    MH_TOKEN JWT via POST /caas/heclr/coire/refreshToken; the assignments API
    wants that JWT as a Bearer header plus the person_xid from its payload.
    Refresh and fetch therefore happen inside one pull() - a pasted token is
    always already dead. Endpoints verified live 2026-08-25.
    """

    name = "connect"
    label = "McGraw Hill Connect"
    base = "https://newconnect.mheducation.com"
    login_hint = (
        "Open https://newconnect.mheducation.com/student/todo while logged "
        "in, then DevTools > Network > right-click the todo request > Copy "
        "as cURL and paste it. The ERIGHTS cookie is the one that matters."
    )

    def _refresh(self, client) -> tuple[str, str]:
        """Returns (bearer_token, person_xid). Raises LoginRequired if the
        ERIGHTS session can no longer mint tokens."""
        import base64
        import json as json_mod

        resp = client.post(f"{self.base}/caas/heclr/coire/refreshToken")
        if resp.status_code != 200:
            raise LoginRequired(
                f"{self.name}: token refresh failed (HTTP {resp.status_code}) "
                f"- the saved session has fully expired. "
                f"Re-capture with: brain sync login {self.name}"
            )
        # Read from the refresh RESPONSE, not the jar: a stale MH_TOKEN from
        # the pasted session lives in the jar under a different domain key and
        # makes the jar lookup ambiguous.
        token = resp.cookies.get("MH_TOKEN") or ""
        if not token:
            raise LoginRequired(
                f"{self.name}: refresh returned no MH_TOKEN cookie.")
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json_mod.loads(base64.urlsafe_b64decode(payload))
            person = claims.get("person_xid") or claims.get("xid") or ""
        except Exception as e:
            raise LoginRequired(f"{self.name}: could not read the token: {e}")
        if not person:
            raise LoginRequired(f"{self.name}: token carries no person id.")
        return token, person

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        from . import http as http_mod

        with http_mod.client(session) as c:
            token, person = self._refresh(c)
            resp = c.get(
                f"{self.base}/openapi/paam/studentAssignments",
                params={"student": person, "userType": "Student"},
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise LoginRequired(
                f"{self.name}: studentAssignments returned HTTP "
                f"{resp.status_code}. Re-capture: brain sync login {self.name}"
            )
        try:
            data = resp.json()
        except ValueError:
            raise LoginRequired(
                f"{self.name}: expected JSON from studentAssignments; the "
                f"session may be mid-expiry. Retry, or re-capture.")
        items = self.parse_assignments(data, courses, window_hi=window_hi)
        if not items:
            raise LoginRequired(
                f"{self.name}: reached the API but parsed no assignments - "
                f"the payload shape may have changed.")
        return items

    def parse_assignments(self, data: dict, courses: list[str],
                          today: date | None = None,
                          window_hi: date | None = None) -> list[PulledItem]:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        # Connect's copied course shells carry stale dues (verified live:
        # ~28 FINC315 items marked LATE with Jan-1-2026 dues and 2022 start
        # dates). The same rolling window the OAKS parsers use keeps them
        # out of the calendar.
        lo, hi = self._window(today, window_hi)
        # Map section id -> configured course by matching course/section names
        # against collection names (FINC 315 / finc-315 / FINC315 all match).
        wanted = {re.sub(r"[^A-Z0-9]", "", c.upper()): c for c in courses}

        def course_for(text: str) -> str | None:
            flat = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
            for code, name in wanted.items():
                if code in flat:
                    return name
            return None

        sections = {s.get("id"): s for s in data.get("sections") or []}
        courses_by_id = {c.get("id"): c for c in data.get("courses") or []}
        sec_course: dict = {}
        for sid, s in sections.items():
            c = courses_by_id.get(s.get("course")) or {}
            sec_course[sid] = (course_for(s.get("name") or "")
                              or course_for(c.get("name") or ""))

        out: list[PulledItem] = []
        for a in data.get("sectionAssignments") or []:
            course = sec_course.get(a.get("section"))
            if not course:
                continue
            due = a.get("dueDate") or a.get("endDate")
            if not due:
                continue
            try:
                dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(tz)
            if not (lo <= local.date() <= hi):
                continue   # copied-shell stale dues (Jan 2026, 2022 history)
            title = (a.get("name") or a.get("title") or "").strip()
            if not title:
                continue
            out.append(PulledItem(
                course=course,
                title=ensure_where(title, "on Connect"),
                date=local.date().isoformat(),
                start_time=local.strftime("%H:%M"),
                kind=_classify(title),
                site=self.name,
                external_id=str(a.get("id") or ""),
            ))
        return out


# VHL Central is a WORLD-LANGUAGE courseware platform, so its one course is
# whichever configured collection is a language course. Match on the department
# prefix rather than hardcoding one student's Spanish section.
_LANG_DEPTS = {"SPAN", "FREN", "GERM", "ITAL", "PORT", "RUSS", "CHIN", "JPNS",
               "JAPN", "ARBC", "ARAB", "LATN", "GREK", "KORE", "HEBR"}


class VhlConnector(Connector):
    """VHL Central Supersite (m3a platform), via the student dashboard page.

    VHL renders server-side: the section page embeds the FULL SEMESTER of
    homework as JSON in a data-assignment-summaries attribute (one entry per
    due date: activity count, estimated time, detail URL). One fetch covers
    everything; verified against the live site 2026-08-25 (40 due dates).

    The session record's base_url stores the section page URL (captured from
    the user's browser paste) - VHL sessions are course-scoped anyway.
    """

    name = "vhl"
    label = "VHL Supersite"
    course = "SPAN200"   # default if the configured courses give no language hint
    # Deadline is "before class" by course policy; matches the existing
    # calendar convention (class starts 13:00).
    due_time = "12:55"
    login_hint = (
        "Open your VHL course (m3a.vhlcentral.com), then DevTools > Network > "
        "right-click the section request > Copy as cURL and paste it."
    )

    def _resolve_course(self, courses: list[str]) -> str:
        """The VHL course is whichever configured collection is a language
        course (SPAN200, FREN102, ...), so a friend's VHL deadlines land in
        THEIR course, not a hardcoded one. Falls back to the default when
        nothing matches."""
        for c in courses or []:
            m = re.match(r"^([A-Za-z]+)", re.sub(r"[^A-Za-z0-9]", "", c))
            if m and m.group(1).upper() in _LANG_DEPTS:
                return c
        return self.course

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        from . import http

        url = session.get("base_url") or ""
        if "/sections/" not in url:
            raise LoginRequired(
                f"{self.name}: the saved session has no section URL. "
                f"Re-capture it with: brain sync login {self.name}"
            )
        html = http.get_html(session, url, self.name)
        items = self.parse_page(html, self._resolve_course(courses))
        if not items:
            raise LoginRequired(
                f"{self.name}: reached the section page but found no "
                f"assignment summaries - the page layout may have changed. "
                f"Re-capture the session or re-check the URL: {url}"
            )
        return items

    def parse_page(self, html: str, course: str | None = None) -> list[PulledItem]:
        import html as html_mod
        import json as json_mod

        course = course or self.course
        m = re.search(r'data-assignment-summaries="([^"]*)"', html)
        if not m:
            return []
        try:
            summaries = json_mod.loads(html_mod.unescape(m.group(1)))
        except ValueError:
            return []
        out: list[PulledItem] = []
        for s in summaries:
            due = s.get("due_date")
            count = s.get("assignment_count")
            if not due or not count:
                continue
            est = s.get("estimated_time") or ""
            est_part = f", est {est}" if est else ""
            out.append(PulledItem(
                course=course,
                title=f"Supersite: {count} activities{est_part}",
                date=due,
                start_time=self.due_time,
                kind="quiz",
                site=self.name,
                external_id=f"vhl-{due}",
                url=s.get("detail_url") or "",
            ))
        return out


class BlendedConnector(Connector):
    """Blended Teaching (FINC380 classbook).

    The platform is self-paced and publishes NO due dates - the classbook page
    carries zero date fields (verified 2026-08-25). There is therefore nothing
    to reconcile: pull() returns [] rather than inventing deadlines. The
    calendar's FINC380 pacing rows come from the syllabus, not from here. This
    connector exists so `brain sync` lists Blended honestly ("0 new") instead
    of failing on a phantom endpoint.
    """

    name = "blended"
    label = "Blended Teaching (self-paced, no due dates)"
    login_hint = (
        "Blended Teaching publishes no due dates (self-paced), so there is "
        "nothing for sync to pull. The FINC380 pacing rows come from the "
        "syllabus instead."
    )

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        return []


REGISTRY: dict[str, Connector] = {
    c.name: c for c in (OaksConnector(), ConnectConnector(), VhlConnector(), BlendedConnector())
}


def get(name: str) -> Connector:
    if name not in REGISTRY:
        raise KeyError(f"Unknown site '{name}'. Known: {', '.join(REGISTRY)}")
    return REGISTRY[name]
