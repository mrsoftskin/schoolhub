"""Switching on self-update for a copy that was installed before it existed.

Re-running the installer and declining the config overwrite is the correct
answer when updating: it preserves the student's courses, search index and
deadlines. But a config generated before self-update existed has no
update_url, and updates._manifest_url reads settings.update_url with NO
fallback to DEFAULT_UPDATE_URL. Without a migration, the people who already
have the app are the only ones who never receive an update, forever.
"""

from __future__ import annotations

import pytest

from brain.setup import DEFAULT_UPDATE_URL, ensure_update_url

OLD = """[settings]
data_dir = "data"
backend = "gemini"
default_model = "gemini-2.5-flash"
models = ["gemini-2.5-flash"]

[[collection]]
name = "FINC389"
roots = ["materials/FINC389"]
assist_level = "full"
"""


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_adds_the_key_to_a_config_that_predates_it(tmp_path):
    p = _write(tmp_path, OLD)
    got = ensure_update_url(p)
    assert got == DEFAULT_UPDATE_URL
    assert 'update_url = ' in p.read_text(encoding="utf-8")


def test_the_migrated_config_still_loads_and_is_wired_up(tmp_path):
    from brain.config import load_config

    p = _write(tmp_path, OLD)
    ensure_update_url(p)
    cfg = load_config(p)
    assert cfg.settings.update_url == DEFAULT_UPDATE_URL
    # And the updater accepts it, rather than treating it as unconfigured.
    from brain.updates import _manifest_url

    assert _manifest_url(cfg) == DEFAULT_UPDATE_URL


def test_everything_else_in_the_file_survives(tmp_path):
    p = _write(tmp_path, OLD)
    ensure_update_url(p)
    out = p.read_text(encoding="utf-8")
    for keep in ('data_dir = "data"', 'backend = "gemini"',
                 'name = "FINC389"', 'roots = ["materials/FINC389"]',
                 'assist_level = "full"'):
        assert keep in out, keep


def test_it_is_idempotent(tmp_path):
    p = _write(tmp_path, OLD)
    assert ensure_update_url(p) == DEFAULT_UPDATE_URL
    before = p.read_text(encoding="utf-8")
    assert ensure_update_url(p) == ""
    assert p.read_text(encoding="utf-8") == before


def test_an_existing_value_is_never_overwritten(tmp_path):
    """Someone pointing at their own manifest keeps it."""
    mine = 'https://example.com/mine.json'
    p = _write(tmp_path, OLD.replace("[settings]",
                                     f'[settings]\nupdate_url = "{mine}"'))
    assert ensure_update_url(p) == ""
    assert mine in p.read_text(encoding="utf-8")


def test_a_commented_out_key_is_not_mistaken_for_a_real_one(tmp_path):
    p = _write(tmp_path, OLD.replace("[settings]",
                                     '[settings]\n# update_url = "https://x/y.json"'))
    # The commented line must not block the real insert.
    assert ensure_update_url(p) == DEFAULT_UPDATE_URL


def test_missing_file_and_missing_settings_section_are_safe(tmp_path):
    assert ensure_update_url(tmp_path / "nope.toml") == ""
    p = _write(tmp_path, '[[collection]]\nname = "X"\n')
    assert ensure_update_url(p) == ""


def test_an_unpublished_build_points_nobody_at_a_dead_url(tmp_path, monkeypatch):
    """DEFAULT_UPDATE_URL is empty in a build the owner never published. The
    migration must then do nothing at all, rather than write an empty or
    broken key into someone's working config."""
    monkeypatch.setattr("brain.setup.DEFAULT_UPDATE_URL", "")
    p = _write(tmp_path, OLD)
    before = p.read_text(encoding="utf-8")
    assert ensure_update_url(p) == ""
    assert p.read_text(encoding="utf-8") == before


def test_a_non_https_url_is_refused_by_the_updater(tmp_path):
    """Belt and braces: even if a config carries http, the updater disables
    itself rather than trusting a manifest an attacker could rewrite."""
    from brain.config import load_config
    from brain.updates import _manifest_url

    p = _write(tmp_path, OLD.replace(
        "[settings]", '[settings]\nupdate_url = "http://example.com/m.json"'))
    with pytest.warns(UserWarning):
        assert _manifest_url(load_config(p)) == ""
