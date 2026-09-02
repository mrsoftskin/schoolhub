"""Marking an assignment done.

The identity rules are the whole feature: a completion must survive a retime
(event ids change), must not leak across a repeating series (42 VHL rows share
one normalized title), and must follow the same "these two titles are the same
deadline" logic the sync reconciler already uses.
"""

from __future__ import annotations

import json

import pytest

from brain import completions as comp
from brain.config import load_config
from conftest import write_config

CAL = ('\n[calendar]\nsemester_start = 2026-08-17\n'
       'semester_end = 2026-12-15\nfixed_csv = "calendar/fixed.csv"\n')


@pytest.fixture
def cfg(tmp_path):
    p = write_config(tmp_path, [
        {"name": "FINC313", "assist_level": "full"},
        {"name": "FINC315", "assist_level": "full"},
        {"name": "SPAN200", "assist_level": "full"},
    ], CAL)
    return load_config(p)


def test_completion_lives_next_to_fixed_csv_not_in_data(cfg):
    """data/ is documented as safe to delete to fix a broken index; a
    completion is the one thing that cannot be rebuilt."""
    p = comp.path_for(cfg)
    assert p.parent == cfg.calendar.fixed_csv.parent
    assert "data" not in p.parts[-2:]


def test_tick_then_read_back(cfg):
    comp.set_done(cfg, course="FINC313", title="Chapter 8 Quiz",
                  date="2026-09-26", done=True)
    assert comp.resolver(cfg)("FINC313", "Chapter 8 Quiz", "2026-09-26")


def test_untick_wins_over_an_earlier_tick(cfg):
    comp.set_done(cfg, course="FINC313", title="Chapter 8 Quiz",
                  date="2026-09-26", done=True)
    comp.set_done(cfg, course="FINC313", title="Chapter 8 Quiz",
                  date="2026-09-26", done=False)
    assert not comp.resolver(cfg)("FINC313", "Chapter 8 Quiz", "2026-09-26")
    # and nothing was destroyed - the history is still on disk
    lines = comp.path_for(cfg).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_a_retimed_deadline_stays_done(cfg):
    """The event id is sha1(...|starts_at), so a retime makes a new id. The
    slot is (course, key, DATE), so it survives - this is the whole reason
    the identity is not the event id."""
    comp.set_done(cfg, course="FINC313",
                  title="Chapter 2 / Chapter 3 Quiz", date="2026-08-31",
                  done=True)
    is_done = comp.resolver(cfg)
    # same day, professor moved it noon -> 10:00, title also gained a suffix
    assert is_done("FINC313",
                   "Chapter 2 / Chapter 3 Quiz- Requires Respondus LockDown "
                   "Browser", "2026-08-31")


def test_one_instance_of_a_repeating_series_only(cfg):
    """All 42 VHL rows share one normalized title; without the date in the
    key, one tick would clear the whole semester."""
    comp.set_done(cfg, course="SPAN200",
                  title="VHL Supersite homework (due before class)",
                  date="2026-08-31", done=True)
    is_done = comp.resolver(cfg)
    assert is_done("SPAN200", "VHL Supersite homework (due before class)",
                   "2026-08-31")
    assert not is_done("SPAN200", "VHL Supersite homework (due before class)",
                       "2026-09-02")


def test_cross_worded_twin_is_the_same_obligation(cfg):
    """OAKS mirrors a Connect assignment under different wording; showing one
    done and its twin not-done would be incoherent."""
    comp.set_done(cfg, course="FINC315", title="Connect: Chapter 2",
                  date="2026-08-31", done=True)
    assert comp.resolver(cfg)("FINC315", "Chapter 2 Assignment (on Connect)",
                              "2026-08-31")


def test_vhl_platform_bucket_counts_as_the_same_day_of_work(cfg):
    """The day-summary and the generic recurring row share neither key nor
    numbers - the platform word is their only link, same as in reconcile."""
    comp.set_done(cfg, course="SPAN200",
                  title="Supersite: 8 activities, est 1h 12m",
                  date="2026-09-04", done=True)
    assert comp.resolver(cfg)("SPAN200",
                              "VHL Supersite homework (due before class)",
                              "2026-09-04")


def test_different_courses_never_share_a_completion(cfg):
    comp.set_done(cfg, course="FINC313", title="Chapter 2 Quiz",
                  date="2026-09-26", done=True)
    assert not comp.resolver(cfg)("FINC315", "Chapter 2 Quiz", "2026-09-26")


def test_an_explicit_untick_is_not_overridden_by_a_fuzzy_twin(cfg):
    """An exact-slot record is the last word: if the student un-ticked THIS
    row, a differently-worded sibling must not tick it back."""
    comp.set_done(cfg, course="FINC315", title="Connect: Chapter 2",
                  date="2026-08-31", done=True)
    comp.set_done(cfg, course="FINC315", title="Chapter 2 Assignment (on Connect)",
                  date="2026-08-31", done=False)
    assert not comp.resolver(cfg)("FINC315",
                                  "Chapter 2 Assignment (on Connect)",
                                  "2026-08-31")


def test_a_truncated_last_line_does_not_lose_the_history(cfg):
    comp.set_done(cfg, course="FINC313", title="Chapter 8 Quiz",
                  date="2026-09-26", done=True)
    p = comp.path_for(cfg)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"v":1,"course":"FINC313","key":"partial')   # power loss
    assert comp.resolver(cfg)("FINC313", "Chapter 8 Quiz", "2026-09-26")


def test_file_is_ascii_so_windows_tools_cannot_mangle_it(cfg):
    """PowerShell text round-trips have corrupted this repo's files twice."""
    comp.set_done(cfg, course="SPAN200",
                  title="Lecci\u00f3n 3: Escritura en clase",
                  date="2026-09-09", done=True)
    raw = comp.path_for(cfg).read_bytes()
    assert raw.isascii()
    assert comp.resolver(cfg)("SPAN200", "Lecci\u00f3n 3: Escritura en clase",
                              "2026-09-09")


def test_missing_file_is_simply_nothing_done(cfg):
    assert not comp.resolver(cfg)("FINC313", "Anything", "2026-09-26")
    assert comp.stats(cfg)["done"] == 0


def test_stats_counts_only_current_state(cfg):
    comp.set_done(cfg, course="FINC313", title="A", date="2026-09-01", done=True)
    comp.set_done(cfg, course="FINC313", title="B", date="2026-09-01", done=True)
    comp.set_done(cfg, course="FINC313", title="B", date="2026-09-01", done=False)
    s = comp.stats(cfg)
    assert s["total"] == 2 and s["done"] == 1
