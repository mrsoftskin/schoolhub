"""What is going on around campus and on King Street.

Deliberately SEPARATE from the deadline calendar, for one measured reason:
several queries select coursework by EXCLUSION rather than inclusion -
`week_load` runs `SELECT ... FROM events WHERE kind != 'admin'` - so any new
kind added to that table would silently inflate the workload chart and the
"what is due" counts. A trivia night is not homework, so socials live here and
the events table is left alone.

Two different shapes of thing live in here, and conflating them is the mistake
this module exists to avoid:

  HAPPY HOURS are not events. They are a standing weekly ATTRIBUTE of a venue
  ("Tue-Fri, 4-7"). Measured during research: a city-wide search for "happy
  hour" across eight days matched exactly ONE venue, because nobody publishes
  them as calendar entries. So they are a hand-curated rule set, refreshed
  occasionally, and they ship WITH the app - the list is public information
  about public businesses, so every friend gets it and an app update delivers
  a newer one.

  SOCIAL EVENTS (a game, a campus lecture, a trivia night) genuinely are dated
  events and come from real feeds.

Every happy hour record carries where it came from and when that was checked,
because the failure that matters is a student showing up to a happy hour that
ended a year ago. An entry with no confirmed days says so rather than
guessing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

# Monday=0, matching datetime.weekday() and the calendar module's convention.
_DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

BUNDLED = Path(__file__).parent / "data" / "happy_hours.toml"


def parse_days(spec: str) -> list[int]:
    """"Tue-Fri" / "Mon, Wed, Fri" / "daily" -> weekday numbers.

    Returns [] for an unparseable or unknown spec, which is the signal that a
    venue's days were never confirmed - the caller must then NOT present it as
    happening today.
    """
    s = (spec or "").strip().lower()
    if not s or s in {"unknown", "?", "varies"}:
        return []
    if s in {"daily", "every day", "everyday", "7 days"}:
        return list(range(7))
    out: set[int] = set()
    for part in s.replace("&", ",").replace(" and ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part or "–" in part:
            lo, _, hi = part.replace("–", "-").partition("-")
            a, b = _DAY_NAMES.get(lo.strip()), _DAY_NAMES.get(hi.strip())
            if a is None or b is None:
                # An unreadable token POISONS the whole spec rather than being
                # skipped. Dropping it silently returned a confident subset -
                # "Mon, Thu (call ahead)" parsed to Monday only - which is the
                # exact confident-but-wrong answer [] exists to prevent.
                return []
            # Wrap around the week: "Fri-Sun" and "Sun-Thu" both make sense.
            n = a
            for _ in range(7):
                out.add(n)
                if n == b:
                    break
                n = (n + 1) % 7
        else:
            d = _DAY_NAMES.get(part)
            if d is None:
                return []
            out.add(d)
    return sorted(out)


def format_days(days: list[int]) -> str:
    """Weekday numbers -> a compact human string ("Tue-Fri", "Mon, Wed")."""
    if not days:
        return "days not confirmed"
    if len(days) == 7:
        return "Daily"
    runs, start, prev = [], days[0], days[0]
    for d in days[1:]:
        if d == prev + 1:
            prev = d
            continue
        runs.append((start, prev))
        start = prev = d
    runs.append((start, prev))
    return ", ".join(_ORDER[a] if a == b else f"{_ORDER[a]}-{_ORDER[b]}"
                     for a, b in runs)


def _parse_time(value: str) -> time | None:
    v = (value or "").strip().lower().replace(" ", "")
    if not v:
        return None
    for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(v, fmt).time()
        except ValueError:
            continue
    return None


@dataclass
class HappyHour:
    name: str
    days: list[int] = field(default_factory=list)
    start: time | None = None
    end: time | None = None
    deals: str = ""
    address: str = ""
    category: str = "bar"
    source: str = ""
    checked: str = ""          # ISO date the details were last confirmed
    note: str = ""
    url: str = ""              # the venue's own site, for a direct link

    @property
    def confirmed(self) -> bool:
        """Enough to tell someone to go: which days, and when."""
        return bool(self.days and self.start)

    def on(self, when: date) -> bool:
        return when.weekday() in self.days

    @staticmethod
    def _clock(t: time) -> str:
        # Built by hand rather than with strftime("%-I"): that directive is
        # POSIX-only and raises on Windows, and this app runs on both.
        hour = t.hour % 12 or 12
        suffix = "AM" if t.hour < 12 else "PM"
        return f"{hour}:{t.minute:02d} {suffix}"

    def window(self) -> str:
        if not self.start:
            return "time not confirmed"
        s = self._clock(self.start)
        if not self.end:
            return f"from {s}"
        return f"{s} - {self._clock(self.end)}"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "address": self.address,
            "category": self.category, "days": format_days(self.days),
            "weekdays": self.days, "window": self.window(),
            # Minutes-since-midnight for the Evening Ruler; None when the
            # curated entry has no confirmed clock time. Additive - nothing
            # existing reads these.
            "start_min": self.start.hour * 60 + self.start.minute if self.start else None,
            "end_min": self.end.hour * 60 + self.end.minute if self.end else None,
            "deals": self.deals, "source": self.source,
            "checked": self.checked, "confirmed": self.confirmed,
            "note": self.note, "url": self.url,
        }


def _load_file(path: Path) -> list[HappyHour]:
    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return []
    out = []
    for v in raw.get("venue", []):
        name = str(v.get("name") or "").strip()
        if not name:
            continue
        out.append(HappyHour(
            name=name,
            days=parse_days(str(v.get("days") or "")),
            start=_parse_time(str(v.get("start") or "")),
            end=_parse_time(str(v.get("end") or "")),
            deals=str(v.get("deals") or "").strip(),
            address=str(v.get("address") or "").strip(),
            category=str(v.get("category") or "bar").strip() or "bar",
            source=str(v.get("source") or "").strip(),
            checked=str(v.get("checked") or "").strip(),
            note=str(v.get("note") or "").strip(),
            url=str(v.get("url") or "").strip(),
        ))
    return out


def user_file(config) -> Path:
    """Where a student's own additions live, beside their calendar."""
    cal = getattr(config, "calendar", None)
    if cal is not None and getattr(cal, "fixed_csv", None):
        return Path(cal.fixed_csv).parent / "happy_hours.toml"
    return Path(config.settings.data_dir) / "happy_hours.toml"


def load(config=None) -> list[HappyHour]:
    """The bundled list, plus the user's own additions (which win on name).

    Shipping the list inside the package means every friend gets it without
    setup, and a refreshed list arrives through the normal app update. A local
    file lets someone add their own spot without waiting for a release.
    """
    venues = {h.name.casefold(): h for h in _load_file(BUNDLED)}
    if config is not None:
        for h in _load_file(user_file(config)):
            venues[h.name.casefold()] = h
    return sorted(venues.values(), key=lambda h: h.name.casefold())


def today(config=None, when: date | None = None) -> list[HappyHour]:
    """Venues whose happy hour runs on `when` - confirmed ones only.

    Unconfirmed entries are deliberately excluded here: listing a venue under
    "today" when nobody established its days is how you send someone across
    town for nothing. They still appear in the full list, labeled.
    """
    day = when or date.today()
    return [h for h in load(config) if h.confirmed and h.on(day)]


# ---- dated social events (campus, athletics, student orgs) --------------
#
# All three are plain ICS, which this app already fetches and parses, so they
# cost almost nothing. They are bundled rather than configured because every
# friend is at the same school - and they land in the SOCIALS store, never in
# the deadline calendar.
#
# Verified live 2026-08-31, with the gotchas that bit during research:
#   - the campus filter param on the .ics is `event_types[]`; passing `type[]`
#     is SILENTLY IGNORED and returns all 265 events with HTTP 200.
#   - calendar.cofc.edu redirects to charleston.edu but DROPS THE PATH, so the
#     charleston.edu host is hardcoded here.
#   - Cougar Connect only ever serves a rolling ~7-day window, so its events
#     accumulate over time rather than backfilling.
FEEDS = [
    {
        "key": "campus",
        "label": "Campus events",
        "url": "https://calendar.charleston.edu/calendar/ics",
        "note": "College of Charleston official calendar (Localist)",
    },
    {
        "key": "athletics",
        "label": "Cougars games",
        "url": "https://cofcsports.com/calendar.ashx/calendar.ics",
        "note": "CofC Athletics, all sports (SIDEARM)",
    },
    {
        "key": "orgs",
        "label": "Student orgs",
        "url": "https://cougarconnect.cofc.edu/events.ics",
        "note": "Cougar Connect / Engage - rolling 7 days only",
    },
]

# Campus categories worth a student's attention. The unfiltered feed is 265
# events, ~41 of them faculty OAKS-bootcamp training.
CAMPUS_EVENT_TYPES = {
    "Student Activities": "44684995342756",
    "Athletics": "44350169269288",
    "Arts & Entertainment": "43578823769253",
    "Reception & Social": "44684988800686",
}


def campus_url(*categories: str) -> str:
    """A campus ICS URL filtered to the given categories (by name)."""
    base = "https://calendar.charleston.edu/calendar/ics"
    ids = [CAMPUS_EVENT_TYPES[c] for c in categories if c in CAMPUS_EVENT_TYPES]
    if not ids:
        return base
    return base + "?" + "&".join(f"event_types%5B%5D={i}" for i in ids)


@dataclass
class SocialEvent:
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    all_day: bool = False
    feed: str = ""
    location: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title, "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "all_day": self.all_day, "feed": self.feed,
            "location": self.location, "url": self.url,
        }


def _parse_feed(path: Path, label: str, lo: date, hi: date) -> list:
    """Read one ICS into SocialEvents, keeping URL and LOCATION.

    Parsed here rather than through calendar.parse_ics_file because that
    returns the deadline `Event` shape, which carries no url or location -
    and widening it would push social concerns into the coursework path this
    module deliberately stays out of.
    """
    from icalendar import Calendar

    out: list[SocialEvent] = []
    cal = Calendar.from_ical(path.read_bytes())
    for c in cal.walk("VEVENT"):
        dtstart = c.get("DTSTART")
        if dtstart is None:
            continue
        raw = dtstart.dt
        if isinstance(raw, datetime):
            start = raw.astimezone().replace(tzinfo=None) if raw.tzinfo else raw
            all_day = False
        elif isinstance(raw, date):
            start = datetime(raw.year, raw.month, raw.day)
            all_day = True
        else:
            continue
        if not (lo <= start.date() <= hi):
            continue
        end = None
        dtend = c.get("DTEND")
        if dtend is not None:
            e = dtend.dt
            if isinstance(e, datetime):
                end = e.astimezone().replace(tzinfo=None) if e.tzinfo else e
            elif isinstance(e, date):
                end = datetime(e.year, e.month, e.day)
        url = str(c.get("URL") or "").strip()
        out.append(SocialEvent(
            title=str(c.get("SUMMARY", "")).strip() or "(untitled)",
            starts_at=start, ends_at=end, all_day=all_day, feed=label,
            location=str(c.get("LOCATION") or "").strip(),
            url=url if url.lower().startswith(("http://", "https://")) else "",
        ))
    return out


def fetch_events(config, *, days: int = 21, feeds: list[dict] | None = None
                 ) -> tuple[list, list[str]]:
    """Pull the bundled social feeds. Returns (events, errors).

    Never raises and never blocks the app: a dead feed is reported and the
    others still load. Deliberately does NOT write to the events table - see
    this module's docstring for why that separation is load-bearing.
    """
    from datetime import timedelta

    from . import feeds as feedmod

    data_dir = Path(config.settings.data_dir)
    lo = date.today()
    hi = lo + timedelta(days=max(1, days))
    out: list[SocialEvent] = []
    errors: list[str] = []
    for spec in (feeds or FEEDS):
        try:
            res = feedmod.fetch(spec["url"], data_dir)
        except Exception as e:
            errors.append(f"{spec['label']}: {type(e).__name__}: {e}")
            continue
        if res.path is None:
            errors.append(f"{spec['label']}: {res.error or 'no data'}")
            continue
        try:
            out.extend(_parse_feed(res.path, spec["label"], lo, hi))
        except Exception as e:
            errors.append(f"{spec['label']}: could not read the feed ({e})")
            continue
        if res.stale:
            errors.append(f"{spec['label']}: using a cached copy (refetch failed)")
    out.sort(key=lambda e: e.starts_at)
    return _dedupe(out), errors


def _dedupe(events: list) -> list:
    """One row per real-world happening.

    The campus calendar and Cougar Connect both carry many of the same
    events, so the raw merge showed "BNB (Bagels 'n' Bibles)" and "The
    Gumball Gamble" twice each. Match on start time plus a loosened title,
    since the two feeds punctuate and capitalize differently
    ("Bagels 'n' Bibles" vs "Bagels 'N' Bibles"). First feed wins, and FEEDS
    is ordered so the official campus calendar is first.
    """
    import re as _re

    seen: set[tuple] = set()
    out = []
    for ev in events:
        slug = _re.sub(r"[^a-z0-9]+", "", ev.title.lower())
        key = (ev.starts_at, slug)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out
