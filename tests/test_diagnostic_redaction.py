"""What the shareable diagnostic report is allowed to contain.

packaging/GUIDE.txt tells the student, in writing, that diagnostic.txt "never
includes the actual keys, passwords, or cookies". launch.py points BOTH
stdout and stderr at data/logs/server.log, so any traceback line in that log
can carry a session cookie, an API key, or a secret calendar URL. The report
used to splice a raw 90-character slice of that log into itself, on exactly
the runs where the report gets sent.

Also covers the persisted calendar status, which stored subscribed feed URLs
verbatim. A Google Calendar "secret address in iCal format" is a bearer
credential to the whole calendar, and the setup guide asks every friend to
paste one in.
"""

from __future__ import annotations

from brain.doctor import _exception_kind
from brain.feeds import label_url

GCAL = "https://calendar.google.com/calendar/ical/s3cr3tT0k3n0987/basic.ics"
D2L = "https://lms.cofc.edu/d2l/le/calendar/feed/user/feed.ics?token=ABC123XYZ"
SECRETS = ("s3cr3tT0k3n0987", "ABC123XYZ", "d2lSessionVal", "sk-ant-",
           "AIzaSy", "abcdef123456")


def _clean(text: str) -> bool:
    return not any(s in text for s in SECRETS)


# ---- the log line summary -------------------------------------------

def test_names_the_exception_type_not_the_message():
    line = (f"ERROR HTTPStatusError: 404 Not Found for url '{GCAL}'")
    out = _exception_kind(line)
    assert out == "HTTPStatusError"
    assert _clean(out)


def test_app_exceptions_are_named_even_without_an_error_suffix():
    """LoginRequired is the most common real failure and does not end in
    'Error', so a suffix-only rule would report it as anonymous."""
    line = "Exception: LoginRequired: oaks d2lSessionVal=abcdef123456 rejected"
    out = _exception_kind(line)
    assert out == "LoginRequired"
    assert _clean(out)


def test_a_cookie_bearing_line_never_leaks():
    line = ("ERROR sending cookies {'d2lSessionVal': 'abcdef123456', "
            "'FedAuth': 'zzz'} to lms.cofc.edu")
    assert _clean(_exception_kind(line))


def test_an_api_key_bearing_line_never_leaks():
    assert _clean(_exception_kind("ERROR bad key sk-ant-api03-abcdef123456"))
    assert _clean(_exception_kind("ERROR AIzaSyABCDEF rejected by the service"))


def test_unrecognized_lines_say_nothing_at_all():
    """When the shape is not recognized, emit a fixed string rather than a
    best-effort excerpt."""
    assert _exception_kind("ERROR something odd sk-ant-1234") == "an error (see the log)"


def test_bare_traceback_is_reported():
    assert _exception_kind("Traceback (most recent call last):") == "Traceback"


def test_empty_and_none_are_safe():
    assert _exception_kind("") == "an error (see the log)"
    assert _exception_kind(None) == "an error (see the log)"


def test_output_is_always_short_and_bounded():
    """Nothing long can escape, whatever the input."""
    monster = "X" * 5000 + "SomeVeryLongCustomError" + "s3cr3tT0k3n0987"
    out = _exception_kind(monster)
    assert len(out) <= 48
    assert _clean(out)


# ---- the stored feed label ------------------------------------------

def test_google_secret_url_is_masked_but_recognizable():
    out = label_url(GCAL)
    assert "s3cr3tT0k3n0987" not in out
    # Still says which service and which file, so a person can tell the rows
    # apart in the calendar status.
    assert "calendar.google.com" in out
    assert out.endswith("basic.ics")


def test_query_string_token_is_dropped():
    out = label_url(D2L)
    assert "ABC123XYZ" not in out
    assert "token" not in out
    assert "lms.cofc.edu" in out


def test_local_paths_are_left_alone():
    """A local .ics path is not a credential and masking it would make the
    calendar status unreadable."""
    assert label_url("C:/Users/x/cal.ics") == "C:/Users/x/cal.ics"
    assert label_url("/home/x/cal.ics") == "/home/x/cal.ics"


def test_malformed_input_does_not_raise():
    for bad in ("", "not a url", "https://"):
        label_url(bad)
