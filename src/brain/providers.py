"""Answer-backend providers: turn a prepared question + system prompt into a
stream of text deltas, one function per LLM vendor.

The `subscription` path (Claude Code login, no key) lives in agentsdk.py. The
three API-KEY paths live here: `api` (Anthropic), `openai`, and `gemini`. All
of them are bound by the SAME upstream gate - retrieval, the similarity floor,
the assist_level check, and the token budget already ran in prepare_ask() - so
a provider here only decides who bills the request and in what wire format the
identical prompt and system instruction are sent.

Keys come from the environment (loaded from .env by env.py). Gemini's free tier
(aistudio.google.com/apikey) is what makes this usable by someone with no paid
LLM plan; OpenAI and Anthropic are pay-as-you-go with their own keys.
"""

from __future__ import annotations

import base64
import os
from typing import Iterator

from .errors import ConfigError, MissingAPIKeyError

# backend name -> (env var holding the key, human label, where to get a key)
_PROVIDERS = {
    "api": ("ANTHROPIC_API_KEY", "Anthropic", "console.anthropic.com/settings/keys"),
    "openai": ("OPENAI_API_KEY", "OpenAI", "platform.openai.com/api-keys"),
    "gemini": ("GEMINI_API_KEY", "Google Gemini", "aistudio.google.com/apikey"),
}


def env_var_for(backend: str) -> str | None:
    """The environment variable that holds this backend's API key, or None for
    a keyless backend (subscription) or an unknown one."""
    p = _PROVIDERS.get(backend)
    return p[0] if p else None


def require_key(backend: str) -> str:
    """Return the API key for `backend`, or raise MissingAPIKeyError naming the
    exact variable and where to get a key."""
    p = _PROVIDERS.get(backend)
    if not p:
        raise ConfigError(f"Unknown API backend {backend!r}.")
    var, label, where = p
    val = os.environ.get(var)
    if not val:
        raise MissingAPIKeyError(
            f"{var} is not set, so the {label} backend cannot run. Get a key at "
            f"{where} and put it in .env as {var}=your-key - or switch "
            f"[settings] backend to one you have a key for."
        )
    return val


def status(backend: str) -> tuple[bool, str]:
    """(can this backend run?, why-not). Key presence only - it does not make a
    network call."""
    try:
        require_key(backend)
        return True, ""
    except MissingAPIKeyError as e:
        return False, str(e)


# ---- per-vendor streaming ------------------------------------------------
# Each takes the prepared question, the system prompt, the chosen model, the
# already-trimmed history (list of {"role": "user"|"assistant", "content": str}
# with a leading user turn), and optional images (list of {"media_type", "data"}
# where data is base64), and yields answer text deltas.


def _stream_anthropic(question, system, model, history, images, max_tokens):
    require_key("api")
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    if images:
        content: list[dict] = [{"type": "text", "text": question}]
        for im in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": im["media_type"],
                           "data": im["data"]},
            })
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": question})
    with client.messages.stream(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
    ) as stream:
        yield from stream.text_stream


def _stream_openai(question, system, model, history, images, max_tokens):
    require_key("openai")
    from openai import OpenAI

    client = OpenAI()
    messages: list[dict] = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]
    if images:
        content: list[dict] = [{"type": "text", "text": question}]
        for im in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{im['media_type']};base64,{im['data']}"},
            })
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": question})
    stream = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


def _stream_gemini(question, system, model, history, images, max_tokens):
    key = require_key("gemini")
    from google import genai

    client = genai.Client(api_key=key)
    # Dict form for contents/config: google-genai coerces it to its own types
    # and the shape is stable across SDK versions (unlike the typed Part.from_*
    # constructors, whose signatures have shifted). Gemini names the assistant
    # turn "model", not "assistant".
    contents = []
    for m in history:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    parts = [{"text": question}]
    for im in (images or []):
        parts.append({"inline_data": {
            "mime_type": im["media_type"],
            "data": base64.b64decode(im["data"]),
        }})
    contents.append({"role": "user", "parts": parts})
    cfg = {"system_instruction": system, "max_output_tokens": max_tokens}
    for chunk in client.models.generate_content_stream(
        model=model, contents=contents, config=cfg,
    ):
        if chunk.text:
            yield chunk.text


_STREAMERS = {
    "api": _stream_anthropic,
    "openai": _stream_openai,
    "gemini": _stream_gemini,
}


def stream(backend, question, system, model, history, images, max_tokens) -> Iterator[str]:
    """Dispatch to the vendor for `backend` and yield answer text deltas."""
    fn = _STREAMERS.get(backend)
    if fn is None:
        raise ConfigError(f"Backend {backend!r} has no API-key streamer.")
    return fn(question, system, model, history, images, max_tokens)
