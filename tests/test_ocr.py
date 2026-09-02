"""OCR pipeline: candidate discovery, companion skip logic, batching."""

from __future__ import annotations

import types
from pathlib import Path

import pymupdf

from brain import ocr as ocrmod


def _make_scanned_pdf(path: Path, pages: int = 2) -> None:
    """A PDF whose pages are images only - no text layer."""
    doc = pymupdf.open()
    # tiny valid PNG (1x1 gray)
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
        "h6FO1AAAAABJRU5ErkJggg==")
    for _ in range(pages):
        page = doc.new_page(width=200, height=200)
        page.insert_image(pymupdf.Rect(10, 10, 190, 190), stream=png)
    doc.save(path)
    doc.close()


def _make_text_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This document has a real text layer. " * 20)
    doc.save(path)
    doc.close()


def _cfg(tmp_path, name="FINC999"):
    col = types.SimpleNamespace(name=name, roots=[str(tmp_path)])
    settings = types.SimpleNamespace(default_model="claude-sonnet-4-6")
    return types.SimpleNamespace(collections=[col], settings=settings)


def test_find_candidates_scanned_vs_text(tmp_path):
    _make_scanned_pdf(tmp_path / "scan.pdf")
    _make_text_pdf(tmp_path / "real.pdf")
    cands = ocrmod.find_candidates(_cfg(tmp_path))
    assert [c.path.name for c in cands] == ["scan.pdf"]
    assert cands[0].kind == "scanned_pdf" and cands[0].pages == 2


def test_find_candidates_skips_up_to_date_companion(tmp_path):
    _make_scanned_pdf(tmp_path / "scan.pdf")
    comp = tmp_path / "scan (transcribed).md"
    comp.write_text("done", encoding="utf-8")
    # companion newer than source -> skipped
    import os
    src_m = (tmp_path / "scan.pdf").stat().st_mtime
    os.utime(comp, (src_m + 10, src_m + 10))
    assert ocrmod.find_candidates(_cfg(tmp_path)) == []


def test_find_candidates_image_html_wrapper(tmp_path):
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    (tmp_path / "wrap.html").write_text(
        '<html><body><img src="pic.png"></body></html>', encoding="utf-8")
    (tmp_path / "real.html").write_text(
        "<html><body>" + "actual words here " * 20 + "</body></html>",
        encoding="utf-8")
    cands = ocrmod.find_candidates(_cfg(tmp_path))
    assert [c.path.name for c in cands] == ["wrap.html"]
    assert cands[0].kind == "image_html"


def test_transcribe_batches_pages(tmp_path, monkeypatch):
    _make_scanned_pdf(tmp_path / "long.pdf", pages=12)
    cands = ocrmod.find_candidates(_cfg(tmp_path))
    calls = []

    def fake_stream(question, system, *, model, history=None, images=None,
                    max_budget_usd=None):
        calls.append((question, len(images or [])))
        yield f"## Page x\ncontent for {len(images or [])} pages"

    import brain.agentsdk as sdk
    monkeypatch.setattr(sdk, "stream", fake_stream)
    text = ocrmod.transcribe(cands[0], _cfg(tmp_path))
    # 12 pages at 5/call -> 3 calls with 5, 5, 2 images
    assert [n for _, n in calls] == [5, 5, 2]
    assert "pages 1 through 5" in calls[0][0]
    assert "pages 11 through 12" in calls[2][0]
    assert text.count("## Page") == 3


def test_write_companion_header(tmp_path):
    _make_scanned_pdf(tmp_path / "scan.pdf")
    c = ocrmod.find_candidates(_cfg(tmp_path))[0]
    out = ocrmod.write_companion(c, "## Page 1\nhello")
    body = out.read_text(encoding="utf-8")
    assert out.name == "scan (transcribed).md"
    assert "Machine transcription" in body and "hello" in body
