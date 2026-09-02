"""`brain calendar link` - connect a Google Calendar after setup.

The wizard asks for this once, in the middle of a long install, and the first
two people through it both missed the prompt. Re-running setup to add it would
mean re-answering everything, so it needs its own command - and that command
must never damage a working config.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from brain.cli import app

runner = CliRunner()

_BASE = """[settings]
data_dir = "data"
backend = "gemini"
default_model = "gemini-2.5-flash"
models = ["gemini-2.5-flash"]

[[collection]]
name = "FINC313"
roots = ["{root}"]
assist_level = "full"
color = "#2a78d6"

[calendar]
semester_start = 2026-08-17
semester_end = 2026-12-15
fixed_csv = "calendar/fixed.csv"
"""

URL = "https://calendar.google.com/calendar/ical/me%40group/private-xyz/basic.ics"


def _cfg(tmp_path, extra=""):
    root = tmp_path / "materials" / "FINC313"
    root.mkdir(parents=True)
    p = tmp_path / "config.toml"
    p.write_text(_BASE.format(root=root.as_posix()) + extra, encoding="utf-8")
    return p


def test_link_adds_the_feed_and_config_still_loads(tmp_path):
    from brain.config import load_config

    p = _cfg(tmp_path)
    res = runner.invoke(app, ["calendar", "link", URL, "--config", str(p)])
    assert res.exit_code == 0, res.output
    assert URL in load_config(p).calendar.ics_urls


def test_link_preserves_an_existing_feed(tmp_path):
    """Replacing the list instead of appending would silently unsubscribe the
    OAKS feed a user already had."""
    from brain.config import load_config

    existing = "https://lms.cofc.edu/d2l/le/calendar/feed/user/feed.ics"
    p = _cfg(tmp_path, extra=f'ics_urls = ["{existing}"]\n')
    res = runner.invoke(app, ["calendar", "link", URL, "--config", str(p)])
    assert res.exit_code == 0, res.output
    urls = load_config(p).calendar.ics_urls
    assert existing in urls and URL in urls


def test_link_is_idempotent(tmp_path):
    from brain.config import load_config

    p = _cfg(tmp_path)
    runner.invoke(app, ["calendar", "link", URL, "--config", str(p)])
    runner.invoke(app, ["calendar", "link", URL, "--config", str(p)])
    assert load_config(p).calendar.ics_urls.count(URL) == 1


def test_link_rejects_a_non_url_without_touching_the_config(tmp_path):
    p = _cfg(tmp_path)
    before = p.read_text(encoding="utf-8")
    res = runner.invoke(app, ["calendar", "link", "my calendar", "--config", str(p)])
    assert res.exit_code == 1
    assert p.read_text(encoding="utf-8") == before


def test_link_normalizes_webcal(tmp_path):
    """Google and Apple both hand out webcal:// links; httpx cannot fetch one."""
    from brain.config import load_config

    p = _cfg(tmp_path)
    res = runner.invoke(app, ["calendar", "link", URL.replace("https://", "webcal://"),
                              "--config", str(p)])
    assert res.exit_code == 0, res.output
    assert load_config(p).calendar.ics_urls == [URL]


def test_link_refuses_when_there_is_no_calendar_section(tmp_path):
    root = tmp_path / "materials" / "FINC313"
    root.mkdir(parents=True)
    p = tmp_path / "config.toml"
    p.write_text(_BASE.format(root=root.as_posix()).split("[calendar]")[0],
                 encoding="utf-8")
    res = runner.invoke(app, ["calendar", "link", URL, "--config", str(p)])
    assert res.exit_code == 1
    assert "brain init" in res.output
