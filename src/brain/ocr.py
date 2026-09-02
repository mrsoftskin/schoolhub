"""OCR pipeline: image-only files become searchable companion transcripts.

Scanned PDFs (a lease with no text layer, a photographed formula sheet) and
image-wrapper HTML files index to zero chunks and land in the Library failure
list. This module finds them, renders their pages, has the vision model
transcribe them to Markdown, and writes a companion "<name> (transcribed).md"
next to the original - the same convention used for the hand-transcribed
formula sheets, so the original stays authoritative and the transcript is
searchable.

Transcription runs on the subscription backend (no API key) and is idempotent:
a candidate whose companion already exists and is newer than the source is
skipped, so re-runs only touch new arrivals.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

# A PDF with fewer text characters than this across ALL pages is a scan.
MIN_TEXT_CHARS = 100
# Render scans at this DPI - enough for print text without huge payloads.
RENDER_DPI = 150
# Pages per vision call. More pages per call = fewer calls but longer answers.
PAGES_PER_CALL = 5
# Hard cap so a 300-page scanned book cannot burn a whole afternoon silently.
MAX_PAGES = 40

TRANSCRIBE_SYSTEM = """\
You transcribe scanned course documents into faithful Markdown. Rules:
- Transcribe EXACTLY what is on each page: text, headings, tables (as
  Markdown tables), formulas (in plain notation), labels in figures.
- Start each page with "## Page N" using the page number you are told.
- Mark handwriting as *(handwritten)* and unreadable spots as [illegible].
- Describe purely visual elements (photos, charts) in one bracketed line,
  e.g. [photo: apartment building exterior].
- NEVER invent, summarize, or complete content that is not visible."""


@dataclass
class OcrCandidate:
    collection: str
    path: Path
    kind: str            # "scanned_pdf" | "image_html"
    pages: int
    companion: Path

    @property
    def display(self) -> str:
        return f"{self.collection}: {self.path.name} ({self.pages}p, {self.kind})"


def _companion_for(path: Path) -> Path:
    return path.with_name(f"{path.stem} (transcribed).md")


def _html_image(path: Path) -> Path | None:
    """The local image file an image-wrapper HTML points at, if any."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # No real text content besides tags?
    stripped = re.sub(r"<[^>]+>", " ", text)
    if len(stripped.split()) > 30:
        return None                      # real HTML document, not a wrapper
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not m:
        return None
    src = m.group(1)
    if src.startswith(("http:", "https:", "data:")):
        return None
    img = (path.parent / src).resolve()
    return img if img.exists() else None


def find_candidates(config: Config, only: str | None = None) -> list[OcrCandidate]:
    """Image-only files across collections that lack an up-to-date transcript."""
    import pymupdf

    out: list[OcrCandidate] = []
    for col in config.collections:
        if only and col.name != only:
            continue
        for root in col.roots:
            root = Path(root)
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() not in (".pdf", ".html", ".htm"):
                    continue
                companion = _companion_for(path)
                if companion.exists() and companion.stat().st_mtime >= path.stat().st_mtime:
                    continue
                if path.suffix.lower() == ".pdf":
                    try:
                        doc = pymupdf.open(path)
                    except Exception:
                        continue
                    try:
                        chars = sum(len(pg.get_text()) for pg in doc)
                        images = sum(len(pg.get_images()) for pg in doc)
                        pages = len(doc)
                    finally:
                        doc.close()
                    if chars < MIN_TEXT_CHARS and images > 0 and pages > 0:
                        out.append(OcrCandidate(col.name, path, "scanned_pdf",
                                                pages, companion))
                else:
                    if _html_image(path) is not None:
                        out.append(OcrCandidate(col.name, path, "image_html",
                                                1, companion))
    return out


# ---- rendering ---------------------------------------------------------

def _render_pdf_pages(path: Path, limit: int = MAX_PAGES) -> list[str]:
    """Each page as base64 PNG, long edge capped, print-readable DPI."""
    import pymupdf

    out: list[str] = []
    doc = pymupdf.open(path)
    try:
        for i, page in enumerate(doc):
            if i >= limit:
                break
            pix = page.get_pixmap(dpi=RENDER_DPI)
            out.append(base64.b64encode(pix.tobytes("png")).decode())
    finally:
        doc.close()
    return out


def _image_file_b64(path: Path) -> tuple[str, str]:
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp", "gif": "image/gif"}.get(
                 path.suffix.lower().lstrip("."), "image/png")
    return media, base64.b64encode(path.read_bytes()).decode()


# ---- transcription -----------------------------------------------------

def transcribe(candidate: OcrCandidate, config: Config,
               model: str | None = None, progress=None) -> str:
    """Full Markdown transcript for one candidate (vision model, batched)."""
    from . import agentsdk

    model = model or config.settings.default_model
    parts: list[str] = []
    if candidate.kind == "image_html":
        img = _html_image(candidate.path)
        media, data = _image_file_b64(img)
        question = "Transcribe this single page. It is page 1."
        text = "".join(agentsdk.stream(
            question, TRANSCRIBE_SYSTEM, model=model,
            images=[{"media_type": media, "data": data}]))
        parts.append(text.strip())
    else:
        pages = _render_pdf_pages(candidate.path)
        for start in range(0, len(pages), PAGES_PER_CALL):
            batch = pages[start:start + PAGES_PER_CALL]
            nums = list(range(start + 1, start + 1 + len(batch)))
            if progress:
                progress(f"pages {nums[0]}-{nums[-1]} of {len(pages)}")
            question = (
                f"Transcribe these {len(batch)} scanned pages. They are pages "
                f"{nums[0]} through {nums[-1]} of the document, in order."
            )
            text = "".join(agentsdk.stream(
                question, TRANSCRIBE_SYSTEM, model=model,
                images=[{"media_type": "image/png", "data": b} for b in batch]))
            parts.append(text.strip())
    return "\n\n".join(p for p in parts if p)


def write_companion(candidate: OcrCandidate, transcript: str) -> Path:
    header = (
        f"# {candidate.path.stem} (transcribed)\n\n"
        f"> Machine transcription of the image-only file "
        f"\"{candidate.path.name}\" so its content is searchable. The "
        f"original stays authoritative; check it for anything critical.\n\n"
    )
    candidate.companion.write_text(header + transcript + "\n", encoding="utf-8")
    return candidate.companion
