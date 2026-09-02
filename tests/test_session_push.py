"""Session push endpoint: the browser extension keeps cookies fresh."""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

from conftest import add_doc, make_core
import brain.web.app as webapp


def _client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "open", "assist_level": "full"}])
    add_doc(tmp_path, "open", "d.md", "hi")
    core.index()
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    return TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}), core


def test_push_saves_session(tmp_path, monkeypatch):
    client, core = _client(tmp_path, monkeypatch)
    r = client.post("/api/session/push",
                    headers={"X-CC-Extension": "1"},
                    json={"site": "oaks", "cookies": {"d2lSessionVal": "abc"}})
    assert r.status_code == 200 and r.json()["site"] == "oaks"
    from brain.connectors import SessionStore
    s = SessionStore(core.config.settings.data_dir).load("oaks")
    assert s["cookies"]["d2lSessionVal"] == "abc"


def test_push_requires_extension_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/session/push",
                    json={"site": "oaks", "cookies": {"a": "b"}})
    assert r.status_code == 403


def test_push_maps_domain_to_site(tmp_path, monkeypatch):
    client, core = _client(tmp_path, monkeypatch)
    r = client.post("/api/session/push", headers={"X-CC-Extension": "1"},
                    json={"domain": "newconnect.mheducation.com",
                          "cookies": {"ERIGHTS": "x"}})
    assert r.status_code == 200 and r.json()["site"] == "connect"


def test_push_rejects_unknown_site_and_empty_cookies(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.post("/api/session/push", headers={"X-CC-Extension": "1"},
                       json={"site": "canvas", "cookies": {"a": "b"}}).status_code == 400
    assert client.post("/api/session/push", headers={"X-CC-Extension": "1"},
                       json={"site": "oaks", "cookies": {}}).status_code == 400


def test_push_preserves_existing_base_url(tmp_path, monkeypatch):
    client, core = _client(tmp_path, monkeypatch)
    from brain.connectors import SessionStore
    store = SessionStore(core.config.settings.data_dir)
    store.save("vhl", {"old": "1"}, base_url="https://m3a.vhlcentral.com/courses/1/sections/2")
    r = client.post("/api/session/push", headers={"X-CC-Extension": "1"},
                    json={"site": "vhl", "cookies": {"live_m3_session": "new"}})
    assert r.status_code == 200
    s = store.load("vhl")
    assert s["base_url"].endswith("/sections/2")   # preserved
    assert s["cookies"] == {"live_m3_session": "new"}


def test_links_pending_and_content_endpoints(tmp_path, monkeypatch):
    import json
    client, core = _client(tmp_path, monkeypatch)
    from pathlib import Path
    from brain import links as L
    data_dir = Path(core.config.settings.data_dir)
    dest = data_dir / "linkdoc" / "Syllabus.txt"
    lid = L.link_id("https://docs.google.com/document/d/ABC/edit")
    (data_dir / "links_pending.json").write_text(json.dumps([
        {"id": lid, "course": "open", "title": "Syllabus", "kind": "google_doc",
         "url": "https://docs.google.com/document/d/ABC/edit", "ext": "txt",
         "dest": str(dest)}]), encoding="utf-8")

    # GET pending never leaks the filesystem dest.
    r = client.get("/api/links/pending")
    assert r.status_code == 200
    items = r.json()["pending"]
    assert len(items) == 1 and "dest" not in items[0] and items[0]["id"] == lid

    # POST content needs the extension header.
    import base64
    body = base64.b64encode(b"real syllabus text").decode()
    assert client.post("/api/links/content", json={"id": lid, "content": body}).status_code == 403
    ok = client.post("/api/links/content", headers={"X-CC-Extension": "1"},
                     json={"id": lid, "content": body})
    assert ok.status_code == 200 and ok.json()["course"] == "open"
    assert dest.read_text(encoding="utf-8") == "real syllabus text"
    # dropped from pending
    assert client.get("/api/links/pending").json()["pending"] == []


def test_app_code_is_served_revalidating_not_heuristically_cached(tmp_path, monkeypatch):
    """StaticFiles sends an ETag but no Cache-Control, so browsers fall back
    to HEURISTIC caching and will serve a stale app.js without asking.

    That is not cosmetic: it hid an entire new tab on a reused browser
    profile, and it would break every self-update by pairing new backend
    code with the previous frontend. `no-cache` still allows a cheap 304 via
    the ETag - it only forbids using the copy without asking first.
    """
    client, _core = _client(tmp_path, monkeypatch)
    for path in ("/", "/app.js", "/style.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path


def test_fonts_and_vendored_libs_keep_a_long_cache(tmp_path, monkeypatch):
    """Content-stable assets should not pay a revalidation round trip."""
    client, _core = _client(tmp_path, monkeypatch)
    r = client.get("/fonts/InterVariable.woff2")
    if r.status_code == 200:
        assert "max-age" in (r.headers.get("cache-control") or "")
