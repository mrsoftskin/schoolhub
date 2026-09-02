"""Provider streaming: the three key-based backends (Anthropic, OpenAI,
Gemini) must format the identical prepared prompt in each vendor's own wire
shape, carry the system instruction, history, and images, and yield plain text
deltas. The vendor SDKs are faked so no network or key is needed - but the real
google.genai / openai / anthropic message objects are still constructed, so a
formatting mistake is caught."""

from __future__ import annotations

import base64
import types as pytypes

import pytest

from brain import providers
from brain.errors import ConfigError, MissingAPIKeyError


# ---- key handling --------------------------------------------------------

def test_env_var_for():
    assert providers.env_var_for("api") == "ANTHROPIC_API_KEY"
    assert providers.env_var_for("openai") == "OPENAI_API_KEY"
    assert providers.env_var_for("gemini") == "GEMINI_API_KEY"
    assert providers.env_var_for("subscription") is None
    assert providers.env_var_for("nope") is None


@pytest.mark.parametrize("backend,var", [
    ("api", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
])
def test_require_key_names_the_variable(backend, var, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingAPIKeyError) as e:
        providers.require_key(backend)
    assert var in str(e.value)


@pytest.mark.parametrize("backend,var", [
    ("api", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
])
def test_status_present_absent(backend, var, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    ok, why = providers.status(backend)
    assert not ok and var in why
    monkeypatch.setenv(var, "k")
    assert providers.status(backend) == (True, "")


# ---- OpenAI --------------------------------------------------------------

class _FakeOpenAI:
    last: dict = {}

    def __init__(self, **kw):
        self.chat = pytypes.SimpleNamespace(completions=self)

    def create(self, model=None, messages=None, max_tokens=None, stream=None):
        _FakeOpenAI.last = dict(model=model, messages=messages, stream=stream)
        for c in ["Hi", " there"]:
            yield pytypes.SimpleNamespace(
                choices=[pytypes.SimpleNamespace(
                    delta=pytypes.SimpleNamespace(content=c))])


def test_openai_stream_formats_and_yields(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "yo"}]
    out = "".join(providers.stream(
        "openai", "the question", "the system", "gpt-4o-mini",
        history, None, 100))
    assert out == "Hi there"
    msgs = _FakeOpenAI.last["messages"]
    assert msgs[0] == {"role": "system", "content": "the system"}
    assert msgs[-1] == {"role": "user", "content": "the question"}
    assert _FakeOpenAI.last["stream"] is True


def test_openai_image_becomes_data_uri(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    img = {"media_type": "image/png", "data": "QUJD"}
    list(providers.stream("openai", "q", "s", "gpt-4o-mini", [], [img], 50))
    content = _FakeOpenAI.last["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


# ---- Gemini --------------------------------------------------------------

class _FakeGemini:
    last: dict = {}

    def __init__(self, api_key=None):
        _FakeGemini.last = {"api_key": api_key}
        self.models = self

    def generate_content_stream(self, model=None, contents=None, config=None):
        _FakeGemini.last.update(
            model=model, contents=contents,
            system=(config or {}).get("system_instruction"))
        for t in ["Ans", "wer"]:
            yield pytypes.SimpleNamespace(text=t)


def test_gemini_stream_maps_roles_and_yields(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr("google.genai.Client", _FakeGemini)
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "yo"}]
    out = "".join(providers.stream(
        "gemini", "the question", "the system", "gemini-2.5-flash",
        history, None, 100))
    assert out == "Answer"
    assert _FakeGemini.last["api_key"] == "g-test"
    assert _FakeGemini.last["system"] == "the system"
    # assistant maps to Gemini's "model" role; the user question is appended.
    roles = [c["role"] for c in _FakeGemini.last["contents"]]
    assert roles == ["user", "model", "user"]
    last_parts = _FakeGemini.last["contents"][-1]["parts"]
    assert any(p.get("text") == "the question" for p in last_parts)


def test_gemini_image_included(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr("google.genai.Client", _FakeGemini)
    img = {"media_type": "image/png",
           "data": base64.b64encode(b"PNGDATA").decode()}
    list(providers.stream("gemini", "q", "s", "gemini-2.5-flash", [], [img], 50))
    parts = _FakeGemini.last["contents"][-1]["parts"]
    assert len(parts) == 2   # one text part + one inline image part
    assert parts[1]["inline_data"]["mime_type"] == "image/png"


# ---- Anthropic (regression: still works through the new module) ----------

class _Ctx:
    def __init__(self, deltas):
        self.text_stream = deltas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeAnthropic:
    last: dict = {}

    def __init__(self, **kw):
        self.messages = self

    def stream(self, model=None, max_tokens=None, system=None, messages=None):
        _FakeAnthropic.last = dict(model=model, system=system, messages=messages)
        return _Ctx(["Cla", "ude"])


def test_anthropic_stream_unchanged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    out = "".join(providers.stream(
        "api", "the question", "the system", "claude-sonnet-4-6",
        [{"role": "user", "content": "hi"}], None, 100))
    assert out == "Claude"
    assert _FakeAnthropic.last["system"] == "the system"
    assert _FakeAnthropic.last["messages"][-1] == {
        "role": "user", "content": "the question"}


def test_unknown_backend_raises():
    with pytest.raises(ConfigError):
        list(providers.stream("carrier-pigeon", "q", "s", "m", [], None, 10))
