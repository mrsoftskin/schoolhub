"""Live key verification in `brain init`.

The wizard used to save whatever was pasted and print "All set.", so a key
that the service later refuses produced a config that looked correct and a
chat that failed days afterward. The common case is not a typo: AI Studio
issues a key to a school Google account and then blocks every request with
it, so nothing short of a real call reveals the problem.
"""

from __future__ import annotations

import os

from brain import setup as setupmod


def test_good_key_passes(monkeypatch):
    seen = {}

    def fake_stream(backend, question, system, model, history, images, max_tokens):
        seen.update(backend=backend, model=model, key=os.environ.get("GEMINI_API_KEY"))
        return iter(["ok"])

    monkeypatch.setattr("brain.providers.stream", fake_stream)
    ok, why = setupmod.verify_backend("gemini", "AIza-good")
    assert ok and why == ""
    # The key under test must be visible to the provider during the call.
    assert seen["key"] == "AIza-good"
    assert seen["backend"] == "gemini"
    assert seen["model"] == "gemini-2.5-flash"


def test_rejected_key_is_reported_not_raised(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("403 API_KEY_SERVICE_BLOCKED")

    monkeypatch.setattr("brain.providers.stream", boom)
    ok, why = setupmod.verify_backend("gemini", "AIza-blocked")
    assert ok is False
    assert "API_KEY_SERVICE_BLOCKED" in why


def test_silent_service_is_a_failure(monkeypatch):
    """A key that authenticates but returns nothing is still unusable."""
    monkeypatch.setattr("brain.providers.stream",
                        lambda *a, **kw: iter([]))
    ok, why = setupmod.verify_backend("openai", "sk-quiet")
    assert ok is False
    assert "nothing back" in why


def test_env_is_restored_afterwards(monkeypatch):
    """The wizard must not leave a rejected key sitting in the environment of
    the running process; .env is the key's real home."""
    monkeypatch.setattr("brain.providers.stream", lambda *a, **kw: iter(["ok"]))
    monkeypatch.setenv("GEMINI_API_KEY", "pre-existing")
    setupmod.verify_backend("gemini", "temporary")
    assert os.environ["GEMINI_API_KEY"] == "pre-existing"


def test_env_is_cleared_when_it_was_unset(monkeypatch):
    monkeypatch.setattr("brain.providers.stream", lambda *a, **kw: iter(["ok"]))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    setupmod.verify_backend("openai", "sk-temp")
    assert "OPENAI_API_KEY" not in os.environ


def test_blank_key_fails_without_calling_out(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("brain.providers.stream",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1))
    ok, why = setupmod.verify_backend("gemini", "")
    assert ok is False and why == "no key given"
    assert called["n"] == 0


def test_subscription_checks_the_cli_instead_of_a_key(monkeypatch):
    """Option 4 has no key, but it can still be missing the Claude Code CLI
    it runs through, which fails exactly the same way on the first question."""
    monkeypatch.setattr("brain.agentsdk.available",
                        lambda: (False, "The `claude` CLI was not found on PATH."))
    ok, why = setupmod.verify_backend("subscription")
    assert ok is False
    assert "claude" in why

    monkeypatch.setattr("brain.agentsdk.available", lambda: (True, ""))
    assert setupmod.verify_backend("subscription") == (True, "")


def test_network_failure_never_raises(monkeypatch):
    """A flaky connection must warn, not kill the wizard mid-setup."""
    def dead(*a, **kw):
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr("brain.providers.stream", dead)
    ok, why = setupmod.verify_backend("api", "sk-ant-whatever")
    assert ok is False and "getaddrinfo" in why
