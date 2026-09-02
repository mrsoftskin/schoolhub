"""The self-diagnosis command.

Two things matter here. First, the report is meant to be SENT to whoever set
the user up, so leaking a key or a session cookie would be a real disclosure -
the redaction tests are the load-bearing ones. Second, every failure has to
carry a fix in words a non-technical person can act on, because the person
reading it has no Claude Code and nobody to ask.
"""

from __future__ import annotations

from pathlib import Path

from brain import doctor as doc


def _cfg(tmp_path, backend="gemini", *, calendar=True, collection=True):
    lines = [
        "[settings]", 'data_dir = "data"', f'backend = "{backend}"',
        'default_model = "gemini-2.5-flash"', 'models = ["gemini-2.5-flash"]', "",
    ]
    if collection:
        root = tmp_path / "materials" / "FINC313"
        root.mkdir(parents=True, exist_ok=True)
        lines += ["[[collection]]", 'name = "FINC313"',
                  f'roots = ["{root.as_posix()}"]', 'assist_level = "full"',
                  'color = "#2a78d6"', ""]
    if calendar:
        lines += ["[calendar]", "semester_start = 2026-08-17",
                  "semester_end = 2026-12-15", 'fixed_csv = "calendar/fixed.csv"', ""]
    p = tmp_path / "config.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---- redaction: the report gets shared, so this must never regress -------

def test_redact_never_returns_the_value():
    secret = "AIzaSyD-ThisIsAFakeKeyValue1234567890xyz"
    out = doc._redact(secret, show_prefix=4)
    assert secret not in out
    assert secret[4:] not in out
    assert f"{len(secret)} chars" in out and "starts AIza" in out


def test_redact_handles_missing_and_empty():
    assert doc._redact(None) == "missing"
    assert doc._redact("") == "empty"
    # a short value must not be spilled whole by the prefix hint
    assert "ab" not in doc._redact("ab", show_prefix=4)


def test_secret_name_detection_covers_the_real_credential_names():
    for name in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "d2lSessionVal", "MH_TOKEN", "ERIGHTS", "CloudFront-Signature",
                 "CloudFront-Policy", "session_cookie"):
        assert doc._looks_secret(name), name
    assert not doc._looks_secret("course")


def test_report_never_prints_the_api_key(tmp_path, monkeypatch):
    key = "AIzaSyFAKE-not-a-real-key-000000000000000"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    report = doc.run(_cfg(tmp_path), offline=True)
    text = report.to_text()
    assert key not in text
    assert "GEMINI_API_KEY present" in text


def test_report_never_prints_session_cookies(tmp_path, monkeypatch):
    from brain.connectors import SessionStore

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    cfg = _cfg(tmp_path)
    cookie = "d2l-secret-cookie-value-do-not-leak-1234567890"
    store = SessionStore(tmp_path / "data")
    store.save("oaks", {"d2lSessionVal": cookie},
               base_url="https://calendar.google.com/private-abc123/basic.ics")
    text = doc.run(cfg, offline=True).to_text()
    assert cookie not in text
    assert "private-abc123" not in text          # ICS urls are bearer secrets
    assert "course logins" in text


def test_tilde_hides_the_username():
    assert "~" in doc._tilde(Path.home() / "Command Center")
    assert Path.home().name not in doc._tilde(Path.home() / "x")


# ---- the checks themselves ----------------------------------------------

def test_missing_key_fails_with_an_actionable_fix(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = doc.run(_cfg(tmp_path), offline=True)
    backend = next(c for c in report.checks if c.name == "AI backend")
    assert backend.status == doc.FAIL
    assert "aistudio.google.com" in backend.fix
    assert "PERSONAL" in backend.fix          # the school-account gotcha
    assert not report.healthy


def test_offline_skips_the_network_key_test(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")

    def explode(*a, **k):                      # any network use fails the test
        raise AssertionError("doctor made a network call in --offline mode")

    monkeypatch.setattr(doc, "_live_backend_test", explode)
    report = doc.run(_cfg(tmp_path), offline=True)
    backend = next(c for c in report.checks if c.name == "AI backend")
    assert backend.status == doc.OK and "not tested" in backend.detail


def test_bad_key_is_reported_as_a_rejected_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    monkeypatch.setattr(doc, "_live_backend_test",
                        lambda cfg, backend: (False, "ClientError: API key not valid"))
    report = doc.run(_cfg(tmp_path), offline=False)
    backend = next(c for c in report.checks if c.name == "AI backend")
    assert backend.status == doc.FAIL
    assert "SCHOOL Google account" in backend.fix


def test_unreadable_config_fails_early_and_stops(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("this is not valid toml [[[", encoding="utf-8")
    report = doc.run(bad, offline=True)
    settings = next(c for c in report.checks if c.name == "settings file")
    assert settings.status == doc.FAIL
    # it must not pretend to check things it could not reach
    assert not any(c.name == "database" for c in report.checks)


def test_missing_calendar_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    report = doc.run(_cfg(tmp_path, calendar=False), offline=True)
    cal = next(c for c in report.checks if c.name == "calendar")
    assert cal.status == doc.WARN and cal.fix


def test_a_course_folder_that_vanished_is_reported(tmp_path, monkeypatch):
    """The friend renames or moves a course folder; search silently returns
    nothing. Name the folder rather than leaving them guessing."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    cfg = _cfg(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "materials" / "FINC313")
    courses = next(c for c in doc.run(cfg, offline=True).checks
                   if c.name == "courses")
    assert courses.status == doc.WARN
    assert "FINC313" in courses.detail


def test_config_with_no_courses_stops_at_the_settings_check(tmp_path, monkeypatch):
    """load_config rejects a config with zero collections, so the doctor must
    report THAT rather than a confusing cascade of later failures."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    report = doc.run(_cfg(tmp_path, collection=False), offline=True)
    settings = next(c for c in report.checks if c.name == "settings file")
    assert settings.status == doc.FAIL and settings.fix
    assert not any(c.name == "courses" for c in report.checks)


def test_every_failure_carries_a_fix(tmp_path, monkeypatch):
    """A FAIL with no remedy is useless to someone who cannot read the code."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = doc.run(_cfg(tmp_path, calendar=False), offline=True)
    for c in report.checks:
        if c.status in (doc.FAIL, doc.WARN):
            assert c.fix, f"{c.name} has no fix text"


def test_report_text_is_plain_and_self_describing(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    text = doc.run(_cfg(tmp_path), offline=True).to_text()
    assert "Command Center - diagnostic report" in text
    assert "safe to send" in text
    assert text.isascii()          # survives email/messaging without mojibake


def test_database_counts_are_reported(tmp_path, monkeypatch):
    from brain.db import connect

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    cfg = _cfg(tmp_path)
    conn = connect(tmp_path / "data" / "brain.db")
    conn.execute(
        "INSERT INTO events (id, course, title, starts_at, ends_at, all_day,"
        " kind, source) VALUES (?,?,?,?,?,?,?,?)",
        ("e1", "FINC313", "Quiz", "2026-09-01T12:00:00", None, 0, "quiz", "csv"))
    conn.commit()
    conn.close()
    report = doc.run(cfg, offline=True)
    db = next(c for c in report.checks if c.name == "database")
    assert "1 calendar items" in db.detail
    assert db.status == doc.WARN          # nothing indexed yet
    assert "Command Center folder" in db.fix


# ---- reaching the config from a friend's Terminal ------------------------

def test_installed_config_path_is_the_mac_app_support_folder(monkeypatch):
    """A friend types the checkup command in a home-directory Terminal, where
    walking up from cwd finds nothing. The installed location is what makes
    the command work at all - and it must match where install.sh writes."""
    import brain.config as cfgmod

    monkeypatch.setattr(cfgmod.sys, "platform", "darwin", raising=False)
    paths = cfgmod.installed_config_paths()
    assert paths
    got = paths[0].as_posix()
    assert got.endswith("Library/Application Support/CommandCenter/config.toml")


def test_find_config_honors_brain_config_env(tmp_path, monkeypatch):
    """The launcher exports BRAIN_CONFIG for the running app; the CLI must
    agree with it rather than picking a different config off the disk."""
    from brain.config import find_config

    target = tmp_path / "elsewhere" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("BRAIN_CONFIG", str(target))
    assert find_config() == target
    # an explicit argument still wins over the environment
    assert find_config(tmp_path / "other.toml") == tmp_path / "other.toml"


def test_report_names_the_app_version(tmp_path, monkeypatch):
    """The first question on any bug report is "which build are you on?" -
    a friend cannot answer that from a Terminal they do not understand."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE0000000000000000000000000000000")
    report = doc.run(_cfg(tmp_path), offline=True)
    row = next(c for c in report.checks if c.name == "app version")
    import brain

    assert row.detail == brain.__version__
    assert row.detail in report.to_text()
