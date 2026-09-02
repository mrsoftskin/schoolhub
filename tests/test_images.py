"""Image-attachment plumbing: server-side validation and the subscription
backend's stream-json envelope. No network; the SDK envelope is inspected as
a plain dict."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from brain.agentsdk import _prompt_stream
from brain.web.app import MAX_IMAGES, _parse_images

PNG = "iVBORw0KGgoAAAANSUhEUg=="  # shape only; validator does not decode


def test_parse_images_none_and_empty():
    assert _parse_images({}) == []
    assert _parse_images({"images": []}) == []


def test_parse_images_good():
    out = _parse_images({"images": [{"media_type": "image/png", "data": PNG}]})
    assert out == [{"media_type": "image/png", "data": PNG}]


def test_parse_images_rejects_bad_type():
    with pytest.raises(HTTPException) as e:
        _parse_images({"images": [{"media_type": "image/tiff", "data": PNG}]})
    assert e.value.status_code == 400


def test_parse_images_rejects_non_list():
    with pytest.raises(HTTPException):
        _parse_images({"images": {"media_type": "image/png", "data": PNG}})


def test_parse_images_rejects_missing_data():
    with pytest.raises(HTTPException):
        _parse_images({"images": [{"media_type": "image/png", "data": ""}]})


def test_parse_images_rejects_too_many():
    many = [{"media_type": "image/png", "data": PNG} for _ in range(MAX_IMAGES + 1)]
    with pytest.raises(HTTPException) as e:
        _parse_images({"images": many})
    assert e.value.status_code == 400


def test_parse_images_rejects_oversize():
    big = {"media_type": "image/png", "data": "A" * (9 * 1024 * 1024)}
    with pytest.raises(HTTPException) as e:
        _parse_images({"images": [big]})
    assert e.value.status_code == 413


def _drain(agen):
    async def go():
        return [m async for m in agen]
    return asyncio.run(go())


def test_prompt_stream_envelope_has_text_then_images():
    imgs = [{"media_type": "image/png", "data": PNG},
            {"media_type": "image/jpeg", "data": PNG}]
    msgs = _drain(_prompt_stream("study this", imgs))
    assert len(msgs) == 1
    m = msgs[0]
    assert m["type"] == "user"
    assert m["message"]["role"] == "user"
    content = m["message"]["content"]
    assert content[0] == {"type": "text", "text": "study this"}
    assert [b["type"] for b in content[1:]] == ["image", "image"]
    assert content[1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": PNG}
    assert content[2]["source"]["media_type"] == "image/jpeg"


# ---- HTTP integration: images travel endpoint -> backend ---------------
# Uses the real FastAPI app and routing with the subscription backend mocked,
# so it proves the wiring (parse -> prepare has_images -> stream_answer images)
# without spawning the claude CLI.

def _img_app(tmp_path, monkeypatch):
    from conftest import add_doc, make_core
    import brain.web.app as webapp
    from brain import agentsdk

    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    add_doc(tmp_path, "open", "d.md", "zorbulon flarnak facts live here.")
    core.index()
    core.config.settings.backend = "subscription"

    captured = {}

    def fake_stream(question, system, *, model, history=None, images=None,
                    max_budget_usd=None):
        captured["images"] = images
        captured["system"] = system
        yield "seen it"

    monkeypatch.setattr(agentsdk, "stream", fake_stream)
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *_a, **_k: core))

    from fastapi.testclient import TestClient
    app = webapp.create_app()
    return TestClient(app, headers={"host": "127.0.0.1"}), captured


def _sse_text(raw):
    out = ""
    for block in raw.split("\n\n"):
        ev, data = None, ""
        for line in block.split("\n"):
            if line.startswith("event: "): ev = line[7:].strip()
            elif line.startswith("data: "): data += line[6:]
        if ev == "delta" and data:
            import json
            out += json.loads(data)["text"]
    return out


def test_quick_ask_delivers_image_to_backend(tmp_path, monkeypatch):
    client, captured = _img_app(tmp_path, monkeypatch)
    r = client.post("/api/ask", json={
        "question": "what is this?",
        "images": [{"media_type": "image/png", "data": PNG}],
    })
    assert r.status_code == 200
    assert _sse_text(r.text) == "seen it"
    assert captured["images"] == [{"media_type": "image/png", "data": PNG}]


def test_quick_ask_image_only_allows_empty_question(tmp_path, monkeypatch):
    client, captured = _img_app(tmp_path, monkeypatch)
    r = client.post("/api/ask", json={
        "images": [{"media_type": "image/png", "data": PNG}],
    })
    assert r.status_code == 200
    assert captured["images"], "image must reach the backend even with no text"


def test_quick_ask_rejects_empty_send(tmp_path, monkeypatch):
    client, _ = _img_app(tmp_path, monkeypatch)
    r = client.post("/api/ask", json={})
    assert r.status_code == 400


def test_quick_ask_rejects_bad_image_type(tmp_path, monkeypatch):
    client, _ = _img_app(tmp_path, monkeypatch)
    r = client.post("/api/ask", json={
        "question": "hi", "images": [{"media_type": "image/svg+xml", "data": PNG}],
    })
    assert r.status_code == 400
