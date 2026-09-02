"""Turning a sync site off.

The sync runs every connector in REGISTRY. A student who only uses OAKS
therefore had three sites they never set up reporting themselves as failures,
and a site whose login is permanently broken (Connect, 401 on token refresh)
warned on every render with nothing the student could do about it. Neither is
a fault worth showing, and neither could be silenced without editing code.
"""

from __future__ import annotations

import types

import pytest

from brain.config import ConfigError
from brain.connectors import REGISTRY
from brain.sync import _active_sites


def _cfg(off):
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(sync_sites_off=off))


def test_nothing_disabled_runs_every_site():
    assert _active_sites(_cfg([]), None) == list(REGISTRY)


def test_disabled_site_is_skipped():
    active = _active_sites(_cfg(["connect"]), None)
    assert "connect" not in active
    assert "oaks" in active
    assert len(active) == len(REGISTRY) - 1


def test_explicit_site_beats_the_disable_list():
    """A disabled site stays testable with `brain sync --site connect`,
    otherwise re-checking one means editing config first."""
    assert _active_sites(_cfg(["connect"]), "connect") == ["connect"]


def test_missing_field_means_nothing_disabled():
    """Several call sites build a minimal settings stub; a missing field must
    not crash the whole sync."""
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace())
    assert _active_sites(cfg, None) == list(REGISTRY)


def test_none_is_treated_as_empty():
    assert _active_sites(_cfg(None), None) == list(REGISTRY)


def test_config_rejects_an_unknown_site_name(tmp_path):
    """A typo would silently disable nothing, and the warning it was meant to
    silence would keep appearing with no clue why."""
    from brain.config import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[settings]\ndata_dir = "data"\nsync_sites_off = ["conect"]\n'
        '\n[[collection]]\nname = "X"\nroots = ["docs"]\n',
        encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert "conect" in str(e.value)
    assert "connect" in str(e.value)      # names the valid options


def test_names_are_normalized(tmp_path):
    from brain.config import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[settings]\ndata_dir = "data"\nsync_sites_off = ["  Connect  "]\n'
        '\n[[collection]]\nname = "X"\nroots = ["docs"]\n'
        'assist_level = "full"\n',
        encoding="utf-8")
    assert load_config(cfg).settings.sync_sites_off == ["connect"]
