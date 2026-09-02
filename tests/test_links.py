"""Link resolution: classify by host, build export/download URLs, notes."""

from __future__ import annotations

from pathlib import Path

from brain import links as L


def test_classify_google_docs_slides_sheets():
    d = L.classify("FINC313", "Syllabus", "https://docs.google.com/document/d/ABC123/edit?tab=t.0")
    assert d.kind == "google_doc" and d.session == "google" and d.ext == "txt"
    assert L.google_export_url(d).endswith("/document/d/ABC123/export?format=txt")
    s = L.classify("FINC313", "Ch1", "https://docs.google.com/presentation/d/XYZ/edit?usp=sharing")
    assert s.kind == "google_slides"
    assert L.google_export_url(s).endswith("/presentation/d/XYZ/export/txt")
    sh = L.classify("X", "Grades", "https://docs.google.com/spreadsheets/d/S9/edit")
    assert sh.kind == "google_sheet" and sh.ext == "csv"


def test_classify_sharepoint_office_types():
    for seg, ext in [("w", "docx"), ("x", "xlsx"), ("p", "pptx"), ("b", "pdf")]:
        t = L.classify("FINC389", "Doc",
                       f"https://cofc-my.sharepoint.com/:{seg}:/g/personal/x/IQAB")
        assert t.kind == "sharepoint" and t.session == "sharepoint" and t.ext == ext
    assert L.sharepoint_download_url(t).endswith("download=1")


def test_classify_reference_and_web():
    ig = L.classify("F", "Reel", "https://www.instagram.com/reel/DEsWJ/")
    assert ig.kind == "reference" and ig.session == ""
    zoom = L.classify("F", "Zoom", "https://cofc.zoom.us/j/8595554983")
    assert zoom.kind == "reference"
    web = L.classify("F", "Stagflation", "https://www.nytimes.com/2026/08/13/opinion/x.html")
    assert web.kind == "web"


def test_safelinks_unwrapped():
    wrapped = ("https://nam11.safelinks.protection.outlook.com/?url="
               "https%3A%2F%2Fwww.nytimes.com%2Fx.html&data=abc")
    t = L.classify("F", "Article", wrapped)
    assert t.kind == "web" and "nytimes.com" in t.url and "safelinks" not in t.url


def test_dest_path_by_kind(tmp_path):
    g = L.classify("F", "My: Syllabus", "https://docs.google.com/document/d/A/edit")
    assert L.dest_path(tmp_path, g).name == "My_ Syllabus.txt"
    r = L.classify("F", "Reel", "https://instagram.com/reel/x")
    assert L.dest_path(tmp_path, r).name == "Reel.md"


def test_note_markdown_variants():
    web = L.classify("F", "Art", "https://example.com/a")
    n = L.note_markdown(web, "extracted article body")
    assert "extracted article body" in n and "Source: https://example.com/a" in n
    ref = L.classify("F", "Reel", "https://instagram.com/reel/x")
    assert "Video or interactive" in L.note_markdown(ref)


def test_login_html_rejected():
    assert L._is_login_html(b"<!doctype html><html>Sign in to your account</html>")
    assert not L._is_login_html(b"Attendance is 10% of your grade")


def test_html_to_text_strips_tags():
    html = b"<html><head><style>x{}</style></head><body><p>Hello &amp; bye</p><script>z</script></body></html>"
    txt = L._html_to_text(html)
    assert "Hello & bye" in txt and "x{}" not in txt and "z" not in txt


# ---- browser-fetch bridge: pending manifest + save_browser_fetched -----

def test_link_id_stable_and_short():
    a = L.link_id("https://docs.google.com/document/d/ABC/edit")
    b = L.link_id("https://docs.google.com/document/d/ABC/edit")
    assert a == b and len(a) == 16 and a != L.link_id("https://other")


def test_browser_fetch_url_by_kind():
    g = L.classify("F", "Syl", "https://docs.google.com/document/d/ABC/edit")
    assert L.browser_fetch_url(g).endswith("export?format=txt")
    sp = L.classify("F", "Doc", "https://cofc-my.sharepoint.com/:w:/g/personal/x/IQAB")
    assert L.browser_fetch_url(sp).endswith("download=1")


def test_save_browser_fetched_matches_and_cleans_stub(tmp_path, monkeypatch):
    import json
    from brain import sync as s
    import types

    root = tmp_path / "FINC313"
    (root / "_synced" / "links").mkdir(parents=True)
    stub = root / "_synced" / "links" / "Syllabus.md"
    stub.write_text("stub", encoding="utf-8")
    dest = root / "_synced" / "links" / "Syllabus.txt"

    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    lid = L.link_id("https://docs.google.com/document/d/ABC/edit")
    (tmp_path / "links_pending.json").write_text(json.dumps([
        {"id": lid, "course": "FINC313", "title": "Syllabus", "kind": "google_doc",
         "url": "https://docs.google.com/document/d/ABC/edit", "ext": "txt",
         "dest": str(dest)}]), encoding="utf-8")

    res = s.save_browser_fetched(cfg, lid, b"Attendance is 10 percent of the grade.")
    assert res["ok"] and res["course"] == "FINC313"
    assert dest.read_text(encoding="utf-8").startswith("Attendance")
    assert not stub.exists()                                   # stub removed
    assert s.load_links_pending(cfg) == []                     # dropped from queue


def test_save_browser_fetched_rejects_login_html(tmp_path):
    import json, types
    from brain import sync as s
    (tmp_path / "links_pending.json").write_text(json.dumps([
        {"id": "x1", "course": "F", "title": "T", "kind": "google_doc",
         "url": "u", "ext": "txt", "dest": str(tmp_path / "o.txt")}]), encoding="utf-8")
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    import pytest
    with pytest.raises(ValueError, match="login"):
        s.save_browser_fetched(cfg, "x1", b"<!doctype html><html>Sign in to your Google Account</html>")


def test_save_browser_fetched_unknown_id(tmp_path):
    import types, pytest
    from brain import sync as s
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ValueError, match="unknown link id"):
        s.save_browser_fetched(cfg, "nope", b"data")


def test_import_downloaded_link(tmp_path):
    import json, types
    from brain import sync as s
    (tmp_path / "links_pending.json").write_text(json.dumps([
        {"id": "abc123", "course": "FINC313", "title": "Syllabus",
         "kind": "google_doc", "url": "u", "ext": "txt",
         "dest": str(tmp_path / "out" / "Syllabus.txt")}]), encoding="utf-8")
    dl = tmp_path / "Downloads" / "cc-links"; dl.mkdir(parents=True)
    (dl / "abc123.txt").write_text("Attendance is 10% of the grade.", encoding="utf-8")
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    r = s.import_downloaded_link(cfg, "abc123", downloads_dir=tmp_path / "Downloads")
    assert r["course"] == "FINC313"
    dest = tmp_path / "out" / "Syllabus.txt"
    assert dest.read_text(encoding="utf-8").startswith("Attendance")
    assert s.load_links_pending(cfg) == []


def test_pull_links_skips_already_imported(tmp_path, monkeypatch):
    """A re-run must not re-queue a Google doc the browser already imported
    (its real file is on disk), or the extension would download it every poll.
    A genuinely new link still gets queued."""
    import types
    from brain import sync as s
    from brain import links as linkmod

    imported_root = tmp_path / "FINC313"
    (imported_root / "_synced" / "links").mkdir(parents=True)
    # The already-imported doc: its real file exists at dest.
    (imported_root / "_synced" / "links" / "Syllabus.txt").write_text(
        "real syllabus content", encoding="utf-8")

    topics = [
        {"course": "FINC313", "title": "Syllabus",
         "url": "https://docs.google.com/document/d/ALREADY/edit",
         "module_path": "Week 1"},
        {"course": "FINC313", "title": "Lecture 3",
         "url": "https://docs.google.com/document/d/NEWONE/edit",
         "module_path": "Week 3"},
    ]

    class _FakeConn:
        def list_links(self, session, courses):
            return topics

    class _FakeStore:
        def __init__(self, *a, **k):
            pass
        def has(self, name):
            return name == "oaks"
        def load(self, name):
            return {"cookies": {}}

    monkeypatch.setattr(s, "get", lambda name: _FakeConn())
    monkeypatch.setattr(s, "SessionStore", _FakeStore)
    monkeypatch.setattr(s, "_collection_root",
                        lambda cfg, course: imported_root)
    # dest_path derives the on-disk name from the title; force titles->files so
    # the imported one resolves to Syllabus.txt (present) and the new one does not.
    monkeypatch.setattr(linkmod, "dest_path",
                        lambda root, t: root / "_synced" / "links" / f"{t.title}.txt")
    # Server-side fetch always fails for Google (that's why the browser fetches).
    monkeypatch.setattr(linkmod, "fetch_content", lambda t, session: (None, ""))

    cfg = types.SimpleNamespace(
        settings=types.SimpleNamespace(data_dir=tmp_path),
        collection_names=lambda: ["FINC313"])
    s.pull_links(cfg, apply=True)

    pending = s.load_links_pending(cfg)
    titles = {p["title"] for p in pending}
    assert "Syllabus" not in titles          # already imported -> not re-queued
    assert "Lecture 3" in titles             # new -> queued for the extension


def test_import_downloaded_link_missing_file(tmp_path):
    import json, types, pytest
    from brain import sync as s
    (tmp_path / "links_pending.json").write_text(json.dumps([
        {"id": "x", "course": "F", "title": "T", "kind": "google_doc",
         "url": "u", "ext": "txt", "dest": str(tmp_path / "o.txt")}]), encoding="utf-8")
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(FileNotFoundError):
        s.import_downloaded_link(cfg, "x", downloads_dir=tmp_path / "Downloads")


def test_import_downloaded_link_rejects_login_page(tmp_path):
    import json, types, pytest
    from brain import sync as s
    (tmp_path / "links_pending.json").write_text(json.dumps([
        {"id": "y", "course": "F", "title": "T", "kind": "google_doc",
         "url": "u", "ext": "txt", "dest": str(tmp_path / "o.txt")}]), encoding="utf-8")
    dl = tmp_path / "Downloads" / "cc-links"; dl.mkdir(parents=True)
    (dl / "y.txt").write_bytes(b"<!doctype html><html>Sign in to Google Account</html>")
    cfg = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ValueError, match="login"):
        s.import_downloaded_link(cfg, "y", downloads_dir=tmp_path / "Downloads")
