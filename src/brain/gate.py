"""assist_level enforcement. This is a hard branch in code, evaluated BEFORE
any API call - never just a prompt suggestion.

Single-collection mode: an 'off' collection refuses before retrieval even runs.
Global mode: the effective level is the MOST RESTRICTIVE among collections
that actually returned chunks. Any 'off' collection with hits blocks the whole
request; any 'explain_only' makes the whole response explain-only. An 'off'
collection that returned nothing does not block.
"""

from __future__ import annotations

from .config import Config
from .errors import AssistBlocked

EXPLAIN_ONLY_INSTRUCTION = """\
ACADEMIC INTEGRITY MODE (explain_only) - this is enforced policy, not a suggestion:
You may explain concepts, walk through the reasoning behind ideas, work
through ANALOGOUS examples you invent yourself, and quiz the user to check
understanding. You MUST refuse to: draft or write anything that will be
submitted for a grade; solve assigned problems, problem sets, or quiz/exam
questions; translate text for submission; or check/confirm the user's answers
to graded work. If a request is for graded work, decline that part explicitly,
say why, and offer the closest allowed alternative (an explanation or a fresh
practice problem of your own design)."""


def check_single(config: Config, collection: str) -> str:
    """Gate for single-collection mode. Returns the effective level, or raises
    AssistBlocked if the collection is 'off'."""
    col = config.collection(collection)
    if col.assist_level == "off":
        raise AssistBlocked(
            [col.name],
            f"Collection '{col.name}' has assist_level = off. "
            f"AI assistance is disabled for it in config.toml.",
        )
    return col.assist_level


def resolve_global(config: Config, collections_with_hits: list[str]) -> tuple[str, list[str]]:
    """Gate for global mode. `collections_with_hits` are the collections that
    returned at least one chunk above the floor.

    Returns (effective_level, restricting_collections) - the second element
    names the collections responsible for an explain_only downgrade, so the
    UI can say which one caused it (spec: TODAY tab). Raises AssistBlocked if
    any hit collection is 'off'."""
    levels = {name: config.collection(name).assist_level for name in collections_with_hits}
    off = sorted(n for n, lv in levels.items() if lv == "off")
    if off:
        raise AssistBlocked(
            off,
            "Global ask blocked: relevant material was found in "
            + ", ".join(f"'{n}'" for n in off)
            + ", which has assist_level = off. Ask a specific collection instead, "
            "or change assist_level in config.toml.",
        )
    restricting = sorted(n for n, lv in levels.items() if lv == "explain_only")
    if restricting:
        return "explain_only", restricting
    return "full", []
