"""Subscribed ICS feeds. The failure modes matter more than the happy path:
a login page returned as HTTP 200 must never be imported as "zero events",
and a dropped connection must fall back to the last good copy rather than
emptying the calendar."""

from __future__ import annotations

from pathlib import Path

import pytest

from brain import feeds
from brain.config import load_config
from conftest import write_config

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//D2L//Brightspace//EN\r\n"
    "BEGIN:VEVENT\r\nUID:1@oaks\r\nSUMMARY:FINC313 Chapter 9 Quiz\r\n"
    "DTSTART:20260918T120000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
).encode()


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, result):
        self._result = result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch(monkeypatch, result):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(result))


def test_fetch_writes_cache(tmp_path, monkeypatch):
    _patch(monkeypatch, FakeResponse(ICS))
    r = feeds.fetch("https://oaks.example/ical/abc", tmp_path)
    assert r.fetched and not r.stale and not r.error
    assert r.path.read_bytes() == ICS


def test_login_page_is_rejected_not_imported_as_empty(tmp_path, monkeypatch):
    """A feed URL that needs auth returns 200 with HTML. Parsing that as an
    empty calendar would silently wipe every previously imported event."""
    _patch(monkeypatch, FakeResponse(b"<!DOCTYPE html><html><body>Sign in</body></html>"))
    r = feeds.fetch("https://oaks.example/ical/abc", tmp_path)
    assert not r.fetched
    assert "not an iCalendar document" in r.error
    assert r.path is None          # nothing to parse


def test_failed_refetch_falls_back_to_cache(tmp_path, monkeypatch):
    _patch(monkeypatch, FakeResponse(ICS))
    first = feeds.fetch("https://oaks.example/ical/abc", tmp_path)
    assert first.fetched

    _patch(monkeypatch, ConnectionError("network is down"))
    second = feeds.fetch("https://oaks.example/ical/abc", tmp_path)
    assert not second.fetched
    assert second.stale
    assert "ConnectionError" in second.error
    assert second.path is not None and second.path.read_bytes() == ICS


def test_first_fetch_failure_has_nothing_to_fall_back_to(tmp_path, monkeypatch):
    _patch(monkeypatch, ConnectionError("no route to host"))
    r = feeds.fetch("https://oaks.example/ical/new", tmp_path)
    assert not r.fetched and not r.stale
    assert r.path is None


def test_oversized_feed_refused(tmp_path, monkeypatch):
    _patch(monkeypatch, FakeResponse(b"BEGIN:VCALENDAR" + b"x" * (9 * 1024 * 1024)))
    r = feeds.fetch("https://oaks.example/ical/huge", tmp_path)
    assert not r.fetched
    assert "larger than" in r.error


def test_urls_in_ics_paths_are_treated_as_feeds(tmp_path):
    cal = """
[calendar]
ics_paths = ["https://oaks.example/ical/abc", "local.ics"]
semester_start = 2026-09-07
semester_end = 2026-09-25
"""
    cfg = load_config(write_config(
        tmp_path, [{"name": "A", "assist_level": "full"}], cal))
    assert cfg.calendar.ics_urls == ["https://oaks.example/ical/abc"]
    assert [p.name for p in cfg.calendar.ics_paths] == ["local.ics"]
    # A missing local file still warns; a URL must not be reported as one.
    assert any("local.ics" in w for w in cfg.warnings)
    assert not any("oaks.example" in w for w in cfg.warnings)


def test_feed_events_land_in_the_calendar(tmp_path, monkeypatch):
    from brain import calendar as cal_mod
    from brain.db import connect

    cal = """
[calendar]
ics_urls = ["https://oaks.example/ical/abc"]
semester_start = 2026-09-07
semester_end = 2026-09-25
"""
    cfg = load_config(write_config(
        tmp_path, [{"name": "FINC313", "assist_level": "full"}], cal))
    _patch(monkeypatch, FakeResponse(ICS))
    conn = connect(tmp_path / "data" / "brain.db")
    report = cal_mod.import_calendar(cfg, conn)
    titles = [r["title"] for r in conn.execute("SELECT title FROM events")]
    assert "FINC313 Chapter 9 Quiz" in titles
    feed_report = next(s for s in report.sources if s.detail.startswith("https://"))
    assert feed_report.imported == 1
    conn.close()


def test_broken_feed_keeps_previously_imported_events(tmp_path, monkeypatch):
    from brain import calendar as cal_mod
    from brain.db import connect

    cal = """
[calendar]
ics_urls = ["https://oaks.example/ical/abc"]
semester_start = 2026-09-07
semester_end = 2026-09-25
"""
    cfg = load_config(write_config(
        tmp_path, [{"name": "FINC313", "assist_level": "full"}], cal))
    conn = connect(tmp_path / "data" / "brain.db")

    _patch(monkeypatch, FakeResponse(ICS))
    cal_mod.import_calendar(cfg, conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='ics'").fetchone()["n"]
    assert before == 1

    # OAKS starts returning its login page.
    _patch(monkeypatch, FakeResponse(b"<html>Sign in</html>"))
    report = cal_mod.import_calendar(cfg, conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM events WHERE source='ics'").fetchone()["n"]
    assert after == before, "a broken feed must not empty the calendar"
    assert "ics" in report.upsert_only()
    conn.close()
