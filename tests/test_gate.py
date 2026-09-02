"""assist_level gate: all three levels in single mode, and global-mode
most-restrictive resolution including the off-blocks-all case."""

from __future__ import annotations

import pytest

from brain.config import load_config
from brain.errors import AssistBlocked
from brain.gate import EXPLAIN_ONLY_INSTRUCTION, check_single, resolve_global
from conftest import write_config

MIX = [
    {"name": "open", "assist_level": "full"},
    {"name": "guarded", "assist_level": "explain_only"},
    {"name": "closed", "assist_level": "off"},
]


@pytest.fixture
def config(tmp_path):
    return load_config(write_config(tmp_path, MIX))


def test_single_full(config):
    assert check_single(config, "open") == "full"


def test_single_explain_only(config):
    assert check_single(config, "guarded") == "explain_only"


def test_single_off_raises(config):
    with pytest.raises(AssistBlocked) as exc:
        check_single(config, "closed")
    assert exc.value.collections == ["closed"]
    assert "closed" in str(exc.value)


def test_global_all_full(config):
    assert resolve_global(config, ["open"]) == ("full", [])


def test_global_most_restrictive_is_explain_only(config):
    level, restricting = resolve_global(config, ["open", "guarded"])
    assert level == "explain_only"
    # The spec requires naming the collection that caused the restriction.
    assert restricting == ["guarded"]


def test_global_any_off_blocks_everything(config):
    with pytest.raises(AssistBlocked) as exc:
        resolve_global(config, ["open", "guarded", "closed"])
    assert exc.value.collections == ["closed"]
    assert "closed" in str(exc.value)


def test_global_off_without_hits_does_not_block(config):
    # 'closed' returned no chunks, so it is not in the hit list - no block.
    assert resolve_global(config, ["open", "guarded"])[0] == "explain_only"
    assert resolve_global(config, []) == ("full", [])


def test_explain_only_instruction_forbids_graded_work():
    for phrase in ("refuse", "graded", "quiz"):
        assert phrase in EXPLAIN_ONLY_INSTRUCTION.lower()
