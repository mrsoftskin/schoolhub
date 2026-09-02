"""Subscription backend: answer through the Claude Agent SDK.

Why this exists: an Anthropic API key bills pay-as-you-go and is entirely
separate from a Claude Pro/Max subscription - the subscription does not carry
API credit. The Agent SDK authenticates with the Claude Code login instead, so
the same questions run on a subscription that is already paid for.

What this is NOT: an agent. Every tool is disabled and max_turns is small, so the
model answers from the excerpts prepare_ask() retrieved and nothing else. It
never reads the disk, never searches the web, and never inherits the user's
CLAUDE.md - identical behavior to the API backend, different meter.

The SDK is async and shells out to the `claude` CLI; the rest of this app is
sync, so the async iterator is drained on a worker thread and handed back as a
plain generator.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterator

from .errors import BackendUnavailable

# Belt and braces: allowed_tools=[] already grants nothing, and this names the
# built-ins explicitly so a future SDK default cannot quietly enable one.
_DISALLOWED = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "TodoWrite",
]

_SENTINEL = object()


def available() -> tuple[bool, str]:
    """Is this backend usable right now? Returns (ok, reason-if-not)."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, ("claude-agent-sdk is not installed. Run: uv sync")
    import shutil

    if not shutil.which("claude"):
        return False, (
            "The `claude` CLI was not found on PATH. The subscription backend "
            "runs through Claude Code - install it, or set "
            "[settings] backend = \"api\" in config.toml to use an API key."
        )
    return True, ""


# Windows caps a whole command line at 32,767 characters, and the SDK passes
# the system prompt as an ARGUMENT. Retrieved excerpts live in that prompt, so
# a big-library question blew the limit and the spawn failed with
# "[WinError 206] The filename or extension is too long" - which the SDK
# reports as "Claude Code not found at <path>", sending you to hunt for a
# binary that is sitting right there and runs fine by hand.
#
# Measured on the live index: one course = 13,711 chars (fine); the Everything
# scope = 34,957 (over). So global chat failed every time on Windows while
# per-course chat worked, which is exactly what it looked like.
#
# Anything above this goes over stdin instead, as a normal user turn. The
# headroom covers the rest of the command line (model, flags, tool lists) and
# the fact that non-ASCII text costs more than one byte per character.
_MAX_SYSTEM_ARGV_CHARS = 8000


def _fold_system_into_prompt(system: str, prompt: str) -> str:
    """Deliver the instructions and excerpts as the user turn instead.

    Kept verbatim and in order, so the precedence assembled in ask.py - the
    citation rules first, the untrusted excerpt text after, the integrity
    policy last - is preserved exactly.
    """
    header = ("You are operating under the following instructions and "
              "reference material. Follow them exactly.")
    fence = "----- end of instructions and reference material -----"
    return f"{header}\n\n{system}\n\n{fence}\n\n{prompt}"


def build_prompt(question: str, history: list[dict] | None) -> str:
    """Fold prior turns into the prompt.

    The Agent SDK takes one prompt string rather than a messages array, and
    resuming by session id would not survive a server restart, so history is
    replayed inline. Conversations stay owned by our database either way.
    """
    if not history:
        return question
    lines = ["Earlier turns in this conversation, for context only:", ""]
    for m in history:
        role = "User" if m.get("role") == "user" else "Assistant"
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    lines += ["", "Current question:", question]
    return "\n".join(lines)


def _prompt_stream(text: str, images: list[dict]):
    """Streaming-input mode: one user message carrying text + image blocks.

    The CLI accepts stream-json input where each line is a user message in the
    Anthropic content-block shape. This is the only way to attach images on the
    subscription backend (the plain string prompt path is text-only).
    """
    async def gen():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    *[
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": im["media_type"],
                                "data": im["data"],
                            },
                        }
                        for im in images
                    ],
                ],
            },
            "parent_tool_use_id": None,
        }

    return gen()


def stream(
    question: str,
    system: str,
    *,
    model: str,
    history: list[dict] | None = None,
    images: list[dict] | None = None,
    max_budget_usd: float | None = None,
) -> Iterator[str]:
    """Yield answer text incrementally, using the Claude Code subscription.

    images, when given, is a list of {"media_type", "data"} where data is
    base64 (no data: URI prefix). They ride on the current turn only.
    """
    ok, reason = available()
    if not ok:
        raise BackendUnavailable(reason)

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        StreamEvent,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        allowed_tools=[],
        disallowed_tools=_DISALLOWED,
        # NOT 1: the model occasionally spends a turn on a (blocked) tool
        # attempt or thinking-only output, and with max_turns=1 the CLI then
        # errors "Reached maximum number of turns" instead of answering -
        # seen live 2026-08-25 on an ordinary chat question. With every tool
        # disallowed, the extra turns cannot act; they only let it recover
        # and produce the answer.
        max_turns=3,
        include_partial_messages=True,
        # Do not inherit the user's CLAUDE.md, project settings, or skills:
        # this app supplies the entire instruction set.
        setting_sources=[],
        permission_mode="bypassPermissions",  # nothing is permitted anyway
        max_budget_usd=max_budget_usd,
    )
    prompt = build_prompt(question, history)
    # Move a large system prompt off the command line (see the constant).
    stream_system = len(system) > _MAX_SYSTEM_ARGV_CHARS
    if stream_system:
        prompt = _fold_system_into_prompt(system, prompt)
        options.system_prompt = None

    out: queue.Queue = queue.Queue()

    def run() -> None:
        import asyncio

        async def go() -> None:
            # Text-only turns keep the plain string prompt; a turn with images
            # switches to streaming-input mode so the image blocks ride along.
            # stdin, not argv, whenever images ride along OR the prompt is
            # too big for a Windows command line.
            prompt_arg = (_prompt_stream(prompt, images)
                          if (images or stream_system) else prompt)
            saw_delta = False
            whole: list[str] = []
            async for msg in query(prompt=prompt_arg, options=options):
                if isinstance(msg, StreamEvent):
                    ev = getattr(msg, "event", None)
                    if isinstance(ev, dict) and ev.get("type") == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            saw_delta = True
                            out.put(delta["text"])
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            whole.append(block.text)
            # If partial streaming produced nothing (older CLI, or a response
            # delivered whole), emit the finished text so the answer is never
            # silently empty.
            if not saw_delta and whole:
                out.put("".join(whole))

        try:
            asyncio.run(go())
        except Exception as e:  # surfaced on the consuming thread
            out.put(e)
        finally:
            out.put(_SENTINEL)

    thread = threading.Thread(target=run, name="agent-sdk-stream", daemon=True)
    thread.start()
    while True:
        item = out.get()
        if item is _SENTINEL:
            break
        if isinstance(item, BaseException):
            # Carry the CAUSE, not just the SDK's rewritten message. The SDK
            # turns any spawn failure into "Claude Code not found at <path>"
            # even when that file exists and runs fine by hand, which sends
            # you looking for a missing binary instead of at the real reason
            # the process would not start.
            causes = []
            c = item.__cause__ or item.__context__
            seen = 0
            while c is not None and seen < 3:
                causes.append(f"{type(c).__name__}: {c}")
                c = c.__cause__ or c.__context__
                seen += 1
            detail = f"{type(item).__name__}: {item}"
            if causes:
                detail += " (caused by " + " <- ".join(causes) + ")"
            raise BackendUnavailable(
                f"Claude Code subscription backend failed: {detail}"
            ) from item
        yield item
