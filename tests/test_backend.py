"""Backend selection: the same gated, cited pipeline can be billed either
through a Claude Code subscription or through an API key. Retrieval, the
similarity floor, and the assist gate must behave identically in both."""

from __future__ import annotations

import pytest

from brain import agentsdk
from brain.ask import prepare_ask, stream_answer
from brain.config import load_config
from brain.errors import ConfigError, MissingAPIKeyError
from conftest import add_doc, make_core, write_config

ONE = [{"name": "open", "assist_level": "full"}]


def _prepared(tmp_path):
    core = make_core(tmp_path, ONE)
    add_doc(tmp_path, "open", "d.md", "zorbulon flarnak is a measure of duration.")
    core.index()
    conn = core.open_db()
    try:
        p = prepare_ask(core.config, conn, core.retriever(conn),
                        "zorbulon flarnak", "open")
    finally:
        conn.close()
    return core, p


def test_default_backend_is_subscription(tmp_path):
    cfg = load_config(write_config(tmp_path, ONE))
    assert cfg.settings.backend == "subscription"


def test_invalid_backend_rejected(tmp_path):
    path = write_config(tmp_path, ONE)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'data_dir = "data"', 'data_dir = "data"\nbackend = "carrier-pigeon"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="backend"):
        load_config(path)


def test_api_backend_without_key_still_fails_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    core, p = _prepared(tmp_path)
    core.config.settings.backend = "api"
    with pytest.raises(MissingAPIKeyError):
        list(stream_answer(core.config, p))


def test_gemini_backend_accepted(tmp_path):
    """A friend with no Claude points the config at Gemini's free tier."""
    path = write_config(tmp_path, ONE)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'data_dir = "data"', 'data_dir = "data"\nbackend = "gemini"'),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.settings.backend == "gemini"


@pytest.mark.parametrize("backend,var", [
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
])
def test_new_backends_gate_on_their_own_key(tmp_path, monkeypatch, backend, var):
    core, p = _prepared(tmp_path)
    core.config.settings.backend = backend
    monkeypatch.delenv(var, raising=False)
    ok, reason = core.backend_status()
    assert not ok and var in reason
    with pytest.raises(MissingAPIKeyError):
        list(stream_answer(core.config, p))


def test_subscription_backend_needs_no_api_key(tmp_path, monkeypatch):
    """The whole point: no key, and the request still goes out."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    core, p = _prepared(tmp_path)
    core.config.settings.backend = "subscription"

    seen = {}

    def fake_stream(question, system, *, model, history=None, images=None,
                    max_budget_usd=None):
        seen.update(question=question, system=system, model=model,
                    history=history, images=images, budget=max_budget_usd)
        yield "dur"
        yield "ation"

    monkeypatch.setattr(agentsdk, "stream", fake_stream)
    out = "".join(stream_answer(core.config, p))
    assert out == "duration"
    assert seen["question"] == "zorbulon flarnak"
    # The gate and the retrieved excerpts must reach the subscription backend
    # exactly as they reach the API one.
    assert "SOURCE EXCERPTS" in seen["system"]
    assert seen["model"] == "claude-sonnet-4-6"


def test_model_validated_before_either_backend_runs(tmp_path, monkeypatch):
    core, p = _prepared(tmp_path)
    called = False

    def boom(*a, **k):
        nonlocal called
        called = True
        yield ""

    monkeypatch.setattr(agentsdk, "stream", boom)
    with pytest.raises(ConfigError, match="not in settings.models"):
        list(stream_answer(core.config, p, model="claude-not-real"))
    assert not called


def test_build_prompt_folds_history():
    assert agentsdk.build_prompt("why?", None) == "why?"
    out = agentsdk.build_prompt("why?", [
        {"role": "user", "content": "what is duration?"},
        {"role": "assistant", "content": "It measures rate sensitivity [1]."},
    ])
    assert "what is duration?" in out
    assert "It measures rate sensitivity [1]." in out
    assert out.rstrip().endswith("why?")
    assert "Current question:" in out


def test_build_prompt_skips_empty_turns():
    out = agentsdk.build_prompt("next", [
        {"role": "user", "content": "  "},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "real turn"},
    ])
    assert "real turn" in out
    assert "Assistant:" not in out


def test_subscription_backend_disables_every_tool():
    """It must answer from our excerpts only - never read the disk itself."""
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep",
                 "WebSearch", "WebFetch", "Task"):
        assert tool in agentsdk._DISALLOWED


def test_backend_status_reports_reason(tmp_path, monkeypatch):
    core, _ = _prepared(tmp_path)
    core.config.settings.backend = "api"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, reason = core.backend_status()
    assert not ok
    assert "ANTHROPIC_API_KEY" in reason

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert core.backend_status() == (True, "")
