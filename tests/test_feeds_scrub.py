"""Feed error text must not carry the feed URL.

A Google Calendar "secret address in iCal format" IS a bearer credential:
anyone holding that URL can read the person's entire private calendar. httpx
embeds the request URL in its error messages, and feeds.fetch() persists that
message into data/calendar_status.json and shows it in the UI. The failure
that produces the message (a wrong, revoked, or expired link) is exactly the
case where the URL is most likely to be passed around while debugging.
"""

from __future__ import annotations

from brain.feeds import scrub

SECRET = "https://calendar.google.com/calendar/ical/abc123DEF456ghi789JKL/basic.ics"
TOKEN = "abc123DEF456ghi789JKL"


def test_full_url_is_removed():
    msg = f"HTTPStatusError: Client error '404 Not Found' for url '{SECRET}'"
    out = scrub(msg, SECRET)
    assert TOKEN not in out
    assert SECRET not in out
    # Still says what went wrong.
    assert "404" in out and "HTTPStatusError" in out


def test_bare_token_is_removed_when_the_url_is_only_partly_echoed():
    """Some errors quote the path but not the scheme and host."""
    msg = f"ReadTimeout: timed out reading /calendar/ical/{TOKEN}/basic.ics"
    out = scrub(msg, SECRET)
    assert TOKEN not in out


def test_short_segments_are_left_alone():
    """Mangling 'calendar' or 'basic.ics' would make errors unreadable without
    protecting anything."""
    msg = "ConnectError: [Errno 11001] getaddrinfo failed"
    assert scrub(msg, SECRET) == msg


def test_hostname_survives_so_the_error_still_names_the_service():
    msg = f"HTTPStatusError: 401 for url '{SECRET}'"
    out = scrub(msg, SECRET)
    assert TOKEN not in out
    # The message is still attributable to a calendar feed failure.
    assert "401" in out


def test_empty_and_missing_text_are_safe():
    assert scrub("", SECRET) == ""


def test_a_url_that_does_not_appear_changes_nothing():
    msg = "ConnectTimeout: connection timed out"
    assert scrub(msg, "https://example.com/x.ics") == msg


def test_fetch_uses_it(tmp_path, monkeypatch):
    """End to end: a real 404 through fetch() must not persist the token."""
    import httpx

    from brain import feeds

    class _Resp:
        content = b""

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                f"Client error '404 Not Found' for url '{SECRET}'",
                request=httpx.Request("GET", SECRET),
                response=httpx.Response(404))

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    res = feeds.fetch(SECRET, tmp_path)
    assert res.fetched is False
    assert TOKEN not in res.error
    assert "404" in res.error
