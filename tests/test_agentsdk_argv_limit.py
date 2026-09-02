"""A large prompt must not be passed on the command line.

Windows caps a whole command line at 32,767 characters and the SDK passes the
system prompt as an ARGUMENT. Retrieved excerpts live in that prompt, so a
big-library question blew the limit and the spawn failed with
"[WinError 206] The filename or extension is too long" - which the SDK then
reported as "Claude Code not found at <path>", pointing at a binary that was
present and ran fine by hand.

Measured on the live index when this was found: one course = 13,711 chars
(worked); the Everything scope = 34,957 (failed every time). The Chat tab
defaults to Everything, so global chat was broken on Windows while per-course
chat looked fine.
"""

from __future__ import annotations

from brain import agentsdk


def test_a_small_system_prompt_still_goes_as_an_argument():
    """The plain path is simpler; only large prompts need stdin."""
    assert len("short instructions") <= agentsdk._MAX_SYSTEM_ARGV_CHARS


def test_the_threshold_leaves_room_for_the_rest_of_the_command_line():
    """The limit is the WHOLE command line - model, flags, tool lists and a
    multi-byte-safe margin all have to fit alongside."""
    assert agentsdk._MAX_SYSTEM_ARGV_CHARS < 32767 / 2


def test_folding_preserves_the_instructions_and_the_question_in_order():
    """ask.py assembles precedence deliberately: citation rules, then the
    untrusted excerpts, then the integrity policy. Folding must not reorder."""
    system = "RULES-FIRST\n\nEXCERPT-TEXT\n\nPOLICY-LAST"
    out = agentsdk._fold_system_into_prompt(system, "THE-QUESTION")
    assert out.index("RULES-FIRST") < out.index("EXCERPT-TEXT") < out.index("POLICY-LAST")
    assert out.index("POLICY-LAST") < out.index("THE-QUESTION")
    assert "end of instructions" in out


def test_a_real_sized_global_prompt_would_exceed_the_argv_limit():
    """Guards the reasoning itself: if this ever stops being true the
    workaround can be reconsidered, but silently dropping it would bring the
    bug back."""
    system = "x" * 34957          # the measured Everything-scope size
    assert len(system) > 32767, "the case this exists for"
    assert len(system) > agentsdk._MAX_SYSTEM_ARGV_CHARS, "so it must stream"


def test_the_error_surfaces_its_underlying_cause():
    """The SDK's message named a missing file that was not missing. Whatever
    actually stopped the process has to reach the user."""
    import inspect

    src = inspect.getsource(agentsdk.stream)
    assert "__cause__" in src and "caused by" in src
