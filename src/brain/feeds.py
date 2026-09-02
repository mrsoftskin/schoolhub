"""Fetch subscribed ICS feeds (OAKS/D2L, Google Calendar) to a local cache.

Why fetch rather than scrape: OAKS is D2L Brightspace, which publishes a
personal iCal subscription URL from its Calendar tool. That is the vendor's
own supported export, so this stays inside the spec's "calendar data arrives
via exported ICS/CSV" rule - no credentialed HTML scraping, no automation
against a login.

Every fetch is cached to disk. A failed refetch falls back to the last good
copy and says so, so a dropped wifi connection degrades to "yesterday's
calendar, loudly flagged" rather than an empty week.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_SECONDS = 20.0
MAX_BYTES = 8 * 1024 * 1024


@dataclass
class FeedResult:
    url: str
    path: Path | None      # local cache file to parse, if any
    fetched: bool          # True = fresh from the network
    stale: bool            # True = refetch failed, using the cached copy
    error: str = ""

    @property
    def label(self) -> str:
        return self.url


def cache_path(data_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Path(data_dir) / "feeds" / f"{digest}.ics"


def label_url(url: str) -> str:
    """A feed URL safe to store and display.

    Keeps the host and the filename, drops the middle, because that middle is
    where the secret lives: a Google Calendar private address is
    .../calendar/ical/<SECRET>/basic.ics and the token alone grants read
    access to the entire calendar. The host is what a person actually needs
    in order to recognize which feed a row refers to.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<feed url hidden>"
    if not parts.scheme or not parts.netloc:
        return url                      # a local path, not a URL
    tail = parts.path.rsplit("/", 1)[-1] or ""
    return f"{parts.scheme}://{parts.netloc}/.../{tail}" if tail         else f"{parts.scheme}://{parts.netloc}/..."


def scrub(text: str, url: str) -> str:
    """Remove a feed URL from error text.

    httpx puts the full request URL in its message: an HTTPStatusError reads
    "Client error '404 Not Found' for url 'https://...'". For a Google Calendar
    private link that URL IS the credential - anyone holding it can read the
    person's whole calendar - and this text is persisted to
    data/calendar_status.json and shown in the UI. A wrong or expired link is
    exactly the case that 404s, so the leak fires precisely when the error
    appears. Verified live 2026-09-01: the secret token survived into the
    stored message.
    """
    if not text:
        return text
    out = text.replace(url, "<feed url hidden>")
    # Also catch the bare token when only part of the URL is echoed back.
    for part in url.split("/"):
        # Long opaque path segments are the secret part; short ones are
        # "calendar", "ical", "basic.ics" and are not worth mangling.
        if len(part) >= 16 and part not in ("calendar.google.com",):
            out = out.replace(part, "<hidden>")
    return out


def fetch(url: str, data_dir: Path) -> FeedResult:
    """Download an ICS feed into the cache. Never raises."""
    dest = cache_path(data_dir, url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    had_cache = dest.exists()

    try:
        import httpx

        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            # A real User-Agent is required, not cosmetic: the CofC athletics
            # feed (SIDEARM, behind a CDN) answers 404 to a request with no
            # UA and 200 to the identical request with one. Measured both
            # ways. Plenty of public feeds sit behind edges that treat an
            # absent UA as a bot.
            resp = client.get(url, headers={
                "Accept": "text/calendar, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
            })
            resp.raise_for_status()
            body = resp.content
    except Exception as e:
        return FeedResult(
            url=url, path=dest if had_cache else None,
            fetched=False, stale=had_cache,
            error=scrub(f"{type(e).__name__}: {e}", url),
        )

    if len(body) > MAX_BYTES:
        return FeedResult(
            url=url, path=dest if had_cache else None,
            fetched=False, stale=had_cache,
            error=f"feed is larger than {MAX_BYTES // (1024 * 1024)} MB; refusing to load",
        )
    head = body[:400].lstrip().upper()
    if not head.startswith(b"BEGIN:VCALENDAR"):
        # A login page or an error page returns 200 with HTML. Importing that
        # as "zero events" would silently empty the calendar.
        return FeedResult(
            url=url, path=dest if had_cache else None,
            fetched=False, stale=had_cache,
            error="response is not an iCalendar document (the URL may need to be "
                  "the private/subscription link, not a page that requires login)",
        )

    dest.write_bytes(body)
    return FeedResult(url=url, path=dest, fetched=True, stale=False)
