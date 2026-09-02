"""First-run setup helpers (`brain init`). The load-bearing test is the
round-trip: whatever render_config writes must pass the real load_config, so a
friend's generated config is never born invalid."""

from __future__ import annotations

from datetime import date

import pytest

from brain import setup as s
from brain.config import load_config


def test_normalize_code():
    assert s.normalize_code("finc 313") == "FINC313"
    assert s.normalize_code("FINC-313") == "FINC313"
    assert s.normalize_code(" span200 ") == "SPAN200"


def test_backend_info_covers_all_backends():
    from brain.config import BACKENDS

    for b in BACKENDS:
        models, default_model, env_var, label, where = s.backend_info(b)
        assert default_model in models          # config requires this invariant
        assert label
        # subscription is keyless; the rest name an env var
        assert (env_var is None) == (b == "subscription")
    with pytest.raises(KeyError):
        s.backend_info("carrier-pigeon")


def test_discover_courses_current_term_offerings_only():
    payload = {"Items": [
        {"OrgUnit": {"Id": 111, "Type": {"Code": "Course Offering"},
                     "Name": "Mgmt of Financial Institutions (FINC-313-01) 2026 Fall"}},
        {"OrgUnit": {"Id": 222, "Type": {"Code": "Course Offering"},
                     "Name": "Intermediate Spanish (SPAN-200-04) 2026 Fall"}},
        {"OrgUnit": {"Id": 333, "Type": {"Code": "Course Offering"},
                     "Name": "Old Money (FINC-201-01) 2025 Fall"}},          # wrong term
        {"OrgUnit": {"Id": 444, "Type": {"Code": "Department"},
                     "Name": "Finance Dept (FINC-000) 2026 Fall"}},          # not an offering
    ]}
    found = s.discover_courses(payload, today=date(2026, 8, 28))
    assert [c["code"] for c in found] == ["FINC313", "SPAN200"]   # sorted
    assert found[0]["ouid"] == 111


def test_discover_courses_dedupes():
    payload = {"Items": [
        {"OrgUnit": {"Id": 1, "Type": {"Code": "Course Offering"},
                     "Name": "A (FINC-313-01) 2026 Fall"}},
        {"OrgUnit": {"Id": 2, "Type": {"Code": "Course Offering"},
                     "Name": "A again (FINC-313-02) 2026 Fall"}},
    ]}
    found = s.discover_courses(payload, today=date(2026, 8, 28))
    assert [c["code"] for c in found] == ["FINC313"]


def test_render_config_round_trips_through_load(tmp_path):
    text = s.render_config(
        name="Alex Rivera", backend="gemini", courses=["FINC313", "SPAN200"],
        latitude=32.78, longitude=-79.93, location_label="Charleston, SC, US")
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)                          # must not raise
    assert cfg.settings.backend == "gemini"
    assert cfg.settings.default_model == "gemini-2.5-flash"
    assert cfg.collection_names() == ["FINC313", "SPAN200"]
    assert cfg.user.name == "Alex Rivera"
    assert cfg.user.has_location
    # roots are relative to the config, not absolute machine paths
    assert not str(cfg.collection("FINC313").roots[0]).startswith("materials")
    assert cfg.collection("FINC313").roots[0].name == "FINC313"
    # a calendar with a deadline CSV is always written, so OAKS deadlines land
    assert cfg.calendar is not None
    assert cfg.calendar.fixed_csv is not None
    assert cfg.calendar.fixed_csv.name == "fixed.csv"
    assert cfg.calendar.semester_end > cfg.calendar.semester_start


def test_render_config_subscribes_google_calendar(tmp_path):
    url = "https://calendar.google.com/calendar/ical/abc%40group/private-xyz/basic.ics"
    text = s.render_config(name="G", backend="gemini", courses=["HIST200"],
                           gcal_ics_url=url)
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)
    assert url in cfg.calendar.ics_urls


def test_term_bounds_by_season():
    from datetime import date

    assert s.term_bounds(date(2026, 9, 1)) == (date(2026, 8, 1), date(2026, 12, 31))
    assert s.term_bounds(date(2026, 2, 1)) == (date(2026, 1, 1), date(2026, 5, 31))
    assert s.term_bounds(date(2026, 6, 15)) == (date(2026, 5, 1), date(2026, 8, 31))


def test_render_config_without_location(tmp_path):
    text = s.render_config(name="No Weather", backend="subscription",
                           courses=["BIOL101"])
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.settings.backend == "subscription"
    assert not cfg.user.has_location
    assert "latitude" not in text


def test_render_config_escapes_quotes(tmp_path):
    text = s.render_config(name='Bobby "Tables"', backend="gemini",
                           courses=["CS101"])
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.user.name == 'Bobby "Tables"'


def test_write_env_key_creates_and_updates(tmp_path):
    env = tmp_path / ".env"
    s.write_env_key(env, "GEMINI_API_KEY", "first")
    assert "GEMINI_API_KEY=first" in env.read_text(encoding="utf-8")
    # a second, unrelated key is preserved
    s.write_env_key(env, "OPENAI_API_KEY", "other")
    # updating the first replaces it in place, exactly once
    s.write_env_key(env, "GEMINI_API_KEY", "second")
    body = env.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=second" in body
    assert "GEMINI_API_KEY=first" not in body
    assert body.count("GEMINI_API_KEY=") == 1
    assert "OPENAI_API_KEY=other" in body


def test_render_config_absolute_materials_root(tmp_path):
    """macOS keeps the app's internals in ~/Library/Application Support (hidden
    from Finder) but course files must live somewhere the student can find."""
    mats = tmp_path / "Command Center"
    text = s.render_config(name="Mac User", backend="gemini",
                           courses=["FINC313"], materials_root=str(mats))
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)
    root = cfg.collection("FINC313").roots[0]
    assert root.is_absolute()
    assert root.name == "FINC313"
    assert "Command Center" in str(root)


def test_course_root_normalizes_separators():
    assert s._course_root("", "FINC313") == "materials/FINC313"
    assert s._course_root("/Users/x/Command Center", "SPAN200") == \
        "/Users/x/Command Center/SPAN200"
    assert s._course_root(r"C:\Users\x\CC" + chr(92), "BIOL101") == "C:/Users/x/CC/BIOL101"
