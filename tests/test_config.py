"""Config loading: happy path, hard validation errors, soft warnings."""

from __future__ import annotations

import pytest

from brain.config import load_config
from brain.errors import ConfigError
from conftest import write_config


def test_happy_path(tmp_path):
    cfg = load_config(write_config(tmp_path, [
        {"name": "A", "assist_level": "full", "color": "#aabbcc"},
        {"name": "B", "assist_level": "explain_only"},
    ]))
    assert cfg.collection_names() == ["A", "B"]
    assert cfg.collection("A").color == "#aabbcc"
    assert cfg.settings.similarity_floor == 0.3
    assert cfg.warnings == []


def test_unknown_assist_level_is_hard_error(tmp_path):
    path = write_config(tmp_path, [{"name": "A", "assist_level": "sometimes"}])
    with pytest.raises(ConfigError, match="assist_level"):
        load_config(path)


def test_duplicate_collection_name_is_hard_error(tmp_path):
    path = write_config(tmp_path, [
        {"name": "A", "assist_level": "full"},
        {"name": "A", "assist_level": "off"},
    ])
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_all_is_reserved(tmp_path):
    path = write_config(tmp_path, [{"name": "all", "assist_level": "full"}])
    with pytest.raises(ConfigError, match="reserved"):
        load_config(path)


def test_bad_color_is_hard_error(tmp_path):
    path = write_config(tmp_path, [
        {"name": "A", "assist_level": "full", "color": "red"},
    ])
    with pytest.raises(ConfigError, match="color"):
        load_config(path)


def test_missing_root_is_warning_not_error(tmp_path):
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}])
    text = path.read_text(encoding="utf-8").replace(
        (tmp_path / "docs" / "A").as_posix(), (tmp_path / "gone").as_posix()
    )
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    assert len(cfg.warnings) == 1
    assert "does not exist" in cfg.warnings[0]


def test_unknown_collection_lookup_fails_loud(tmp_path):
    cfg = load_config(write_config(tmp_path, [{"name": "A", "assist_level": "full"}]))
    with pytest.raises(ConfigError, match="Unknown collection"):
        cfg.collection("nope")


def test_utf8_bom_in_config_is_tolerated(tmp_path):
    """Windows editors and `Set-Content -Encoding utf8` prepend a BOM; tomllib
    rejects it as 'invalid statement, line 1'."""
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}])
    raw = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    cfg = load_config(path)
    assert cfg.collection_names() == ["A"]


def test_user_section(tmp_path):
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}])
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[user]\nname = "Carson"\nlatitude = 32.78\nlongitude = -79.93\n'
          'location_label = "Charleston, SC"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.user.name == "Carson"
    assert cfg.user.has_location
    assert cfg.user.location_label == "Charleston, SC"


def test_user_location_requires_both_coordinates(tmp_path):
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}])
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[user]\nlatitude = 32.78\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="together"):
        load_config(path)


def test_missing_config_file_fails_loud(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_bad_recurring_weekday_is_hard_error(tmp_path):
    bad_cal = """
[calendar]
semester_start = 2026-09-07
semester_end = 2026-09-25

[[calendar.recurring]]
course = "A"
title = "class"
weekdays = ["Funday"]
start = "09:00"
end = "09:50"
"""
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}], bad_cal)
    with pytest.raises(ConfigError, match="Funday"):
        load_config(path)


def test_semester_end_before_start_is_hard_error(tmp_path):
    bad_cal = """
[calendar]
semester_start = 2026-09-07
semester_end = 2026-09-01
"""
    path = write_config(tmp_path, [{"name": "A", "assist_level": "full"}], bad_cal)
    with pytest.raises(ConfigError, match="before"):
        load_config(path)


def test_default_excludes_skip_os_junk(tmp_path):
    """macOS AppleDouble sidecars and __MACOSX folders match the include globs
    and would otherwise index as garbage on a Mac."""
    from conftest import write_config
    from brain.config import load_config

    cfg = load_config(write_config(tmp_path, [{"name": "c1", "assist_level": "full"}]))
    ex = cfg.collection("c1").exclude
    for pat in ("**/._*", "**/__MACOSX/**", "**/.DS_Store", "**/Thumbs.db"):
        assert pat in ex
