"""Shared types and session handling for credentialed assignment sync.

The user is the account holder pulling their OWN data from OAKS/Connect/VHL/
Blended. Rather than store a password or automate a login, each connector
reuses the browser session the user already established: cookies are captured
once (pasted or read from the local browser) and replayed against the site's
own JSON endpoints. Sessions expire in hours, so `pull()` fails loud with a
"re-login" message rather than returning an empty, misleading result.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Words that already tell the reader where an assignment happens. If none of
# these appear in a pulled title, the connector appends its platform so every
# calendar row answers "where do I do this?" (user rule, 2026-08-25).
_LOCATION_HINTS = re.compile(
    r"(?i)\b(oaks|d2l|brightspace|connect|smartbook|vhl|supersite|blended|"
    r"in.?class|en clase|in person|mct|my conversation trainer|talk abroad|"
    r"zoom|lockdown|respondus|dropbox)\b"
)


def ensure_where(title: str, where: str) -> str:
    """Append '(where)' unless the title already names a platform/location."""
    if _LOCATION_HINTS.search(title):
        return title
    return f"{title} ({where})"


@dataclass
class PulledItem:
    """One dated item scraped from a platform, normalized to calendar shape."""
    course: str
    title: str
    date: str | None                 # YYYY-MM-DD, or None if undated
    start_time: str = ""             # HH:MM 24h, or ""
    end_time: str = ""
    all_day: bool = False
    kind: str = "quiz"               # exam|quiz|project|admin
    site: str = ""                   # oaks|connect|vhl|blended
    external_id: str = ""            # stable per-site id when available
    url: str = ""
    # Every HH:MM the source legitimately associates with this item (a D2L
    # quiz has an availability-open, a due, and a close time). A stored row
    # holding ANY of these is current; a stored time outside the set means
    # the deadline was retimed at the source. Empty = only start_time known.
    known_times: tuple = ()
    # Every YYYY-MM-DD this record spans (open/due/close dates). A calendar
    # event on one of these dates is THIS item's availability window and
    # folds into it; an event on any other date is a separate occurrence and
    # must survive on its own (drip-released repeats never reach the quiz
    # API, so folding by title alone would silently delete them).
    known_dates: tuple = ()

    def csv_row(self) -> list[str]:
        allday = "true" if self.all_day else "false"
        st = "" if self.all_day else self.start_time
        et = "" if self.all_day else self.end_time
        return [self.course, self.title, self.date or "", st, et, allday, self.kind]


class LoginRequired(Exception):
    """The stored session is missing or expired; the user must re-capture it."""


class SessionStore:
    """Cookies persisted per site under data/sessions/<site>.json.

    Stored as a plain name->value map plus a captured-at timestamp. Never
    committed (data/ is gitignored); it is the user's own session on the
    user's own machine.
    """

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir) / "sessions"

    def _path(self, site: str) -> Path:
        return self.dir / f"{site}.json"

    def save(self, site: str, cookies: dict[str, str],
             base_url: str = "") -> Path:
        """Store a session. A caller that does not supply base_url KEEPS the
        stored one rather than clearing it.

        That default is load-bearing. VHL's base_url is its section URL, and
        the connector cannot work without it - but `brain sync login vhl`
        saves cookies alone, so re-logging in used to wipe the URL and the
        resulting error told the user to... re-log in, which could never fix
        it. Observed live: a session refreshed at 12:31 left base_url "" and
        VHL sync dead.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(site)
        if not base_url and p.exists():
            try:
                base_url = json.loads(
                    p.read_text(encoding="utf-8")).get("base_url", "") or ""
            except (OSError, ValueError):
                base_url = ""
        p.write_text(json.dumps({
            "site": site, "base_url": base_url,
            "captured_at": int(time.time()), "cookies": cookies,
        }, indent=2), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        return p

    def load(self, site: str) -> dict:
        p = self._path(site)
        if not p.exists():
            raise LoginRequired(
                f"No saved session for '{site}'. Run: brain sync login {site}"
            )
        return json.loads(p.read_text(encoding="utf-8"))

    def has(self, site: str) -> bool:
        return self._path(site).exists()

    def age_hours(self, site: str) -> float | None:
        if not self.has(site):
            return None
        data = json.loads(self._path(site).read_text(encoding="utf-8"))
        return (time.time() - data.get("captured_at", 0)) / 3600.0


class Connector:
    """Base class for one platform. Subclasses implement pull()."""
    name: str = ""
    label: str = ""
    login_hint: str = ""
    # Reused course shells serve YEARS of copy-forward history (2022 dates,
    # last-spring dues); only a window around today is real. window_hi
    # additionally clamps the horizon to the configured semester end, so a
    # shell carrying next-term placeholder dates cannot seed the calendar.
    WINDOW_BACK_DAYS = 30
    WINDOW_AHEAD_DAYS = 240

    def _window(self, today=None, window_hi=None):
        """(lo, hi) date bounds for accepting a pulled item."""
        from datetime import date as _date, timedelta as _td

        anchor = today or _date.today()
        lo = anchor - _td(days=self.WINDOW_BACK_DAYS)
        hi = anchor + _td(days=self.WINDOW_AHEAD_DAYS)
        if window_hi is not None and window_hi < hi:
            hi = window_hi
        return lo, hi

    def pull(self, session: dict, courses: list[str], *,
             window_hi=None) -> list[PulledItem]:
        """Fetch dated items using the stored session. Raise LoginRequired if
        the session is rejected. `courses` are the configured collection names,
        used to tag items to a course. `window_hi` (a date) caps how far ahead
        a pulled item may be dated (normally semester end + grace)."""
        raise NotImplementedError
