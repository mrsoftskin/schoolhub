"""Resolve OAKS 'Link' content topics into searchable local notes.

Nothing is skipped: EVERY link becomes a note under <course>/_synced/links/
carrying its course, title, module, and URL, so it is findable in chat. On top
of that, links whose content can be extracted get their full text embedded:

  Google Docs/Slides/Sheets  -> exported to text (needs the 'google' session)
  SharePoint / OneDrive      -> file downloaded in place (needs 'sharepoint')
  News / public web articles -> HTML fetched and stripped to text
  Instagram / Zoom / games   -> reference stub only (no extractable text), but
                                still indexed with title + link

Google/SharePoint sessions come from the browser extension (same mechanism as
the course sites). A link whose session is missing still gets its stub note,
so the catalog of links is always complete even before you log in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

_GOOGLE_DOC = re.compile(r"docs\.google\.com/(document|presentation|spreadsheets)/d/([A-Za-z0-9_-]+)")

# Hosts with no extractable text (video/interactive/live): still noted, not fetched.
_REFERENCE_HOSTS = (
    "instagram.com", "youtube.com", "youtu.be", "zoom.us", "jeopardylabs.com",
    "tiktok.com", "facebook.com", "twitter.com", "x.com", "vimeo.com",
)


@dataclass
class LinkTarget:
    course: str
    title: str
    url: str
    module: str
    kind: str            # google_doc|google_slides|google_sheet|sharepoint|web|reference
    session: str         # stored session it needs ("google"/"sharepoint"/"")
    ext: str             # extension of the downloaded artifact (docx/xlsx/... or "")


def link_id(url: str) -> str:
    """Stable short id for a link, used to match a browser-fetched result back
    to its pending entry."""
    import hashlib

    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def browser_fetch_url(target: "LinkTarget") -> str:
    """The URL the extension should fetch in-browser for this target."""
    if target.kind.startswith("google"):
        return google_export_url(target)
    if target.kind == "sharepoint":
        return sharepoint_download_url(target)
    return target.url


def _unwrap_safelinks(url: str) -> str:
    if "safelinks.protection.outlook.com" in url:
        q = parse_qs(urlparse(url).query)
        if q.get("url"):
            return unquote(q["url"][0])
    return url


def classify(course: str, title: str, url: str, module: str = "") -> LinkTarget:
    url = _unwrap_safelinks(url.strip())
    host = (urlparse(url).hostname or "").lower()

    m = _GOOGLE_DOC.search(url)
    if m:
        kind = {"document": "google_doc", "presentation": "google_slides",
                "spreadsheets": "google_sheet"}[m.group(1)]
        ext = {"google_doc": "txt", "google_slides": "txt",
               "google_sheet": "csv"}[kind]
        return LinkTarget(course, title, url, module, kind, "google", ext)

    if host.endswith("sharepoint.com"):
        seg = re.search(r"sharepoint\.com/:([wxpb]):/", url)
        ext = {"w": "docx", "x": "xlsx", "p": "pptx", "b": "pdf"}.get(
            seg.group(1) if seg else "", "bin")
        return LinkTarget(course, title, url, module, "sharepoint", "sharepoint", ext)

    if any(h in host for h in _REFERENCE_HOSTS):
        return LinkTarget(course, title, url, module, "reference", "", "")

    # D2L quicklinks are relative (/d2l/...): prepend the OAKS host and read
    # them with the OAKS session (they redirect to real content/quiz pages).
    if url.startswith("/d2l/") or url.startswith("/content/"):
        return LinkTarget(course, title, "https://lms.cofc.edu" + url,
                          module, "web", "oaks", "")
    if "lms.cofc.edu" in host or "brightspace.com" in host:
        return LinkTarget(course, title, url, module, "web", "oaks", "")
    if "blended-teaching.com" in host:
        return LinkTarget(course, title, url, module, "web", "blended", "")

    # A public article: read it with no session.
    return LinkTarget(course, title, url, module, "web", "", "")


# ---- resolvers ---------------------------------------------------------

def google_export_url(target: LinkTarget) -> str:
    m = _GOOGLE_DOC.search(target.url)
    doc_type, doc_id = m.group(1), m.group(2)
    if doc_type == "document":
        return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    if doc_type == "presentation":
        return f"https://docs.google.com/presentation/d/{doc_id}/export/txt"
    return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv"


def sharepoint_download_url(target: LinkTarget) -> str:
    sep = "&" if "?" in target.url else "?"
    return f"{target.url}{sep}download=1"


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")


def _is_login_html(body: bytes) -> bool:
    head = body[:2000].lower()
    return (b"<html" in head or b"<!doctype html" in head) and any(
        s in head for s in (b"sign in", b"password", b"log in", b"accounts.google",
                            b"login.microsoftonline"))


def _html_to_text(body: bytes) -> str:
    try:
        html = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as _h
    text = _h.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_content(target: LinkTarget, session: dict | None,
                  timeout: float = 60.0) -> tuple[bytes | None, str]:
    """Return (bytes_or_None, note). bytes is the artifact to save (google/
    sharepoint) or None. note is extracted text for web pages, else ''."""
    import httpx

    cookies = (session or {}).get("cookies") or {}
    if target.kind == "reference":
        return None, ""
    if target.kind.startswith("google"):
        url = google_export_url(target)
    elif target.kind == "sharepoint":
        url = sharepoint_download_url(target)
    else:
        url = target.url
    with httpx.Client(timeout=timeout, follow_redirects=True, cookies=cookies,
                      headers={"User-Agent": _UA}) as c:
        try:
            r = c.get(url)
        except Exception:
            return None, ""
    if r.status_code != 200:
        return None, ""
    ct = r.headers.get("content-type", "")
    body = r.content
    if target.kind.startswith("google"):
        if "text/html" in ct and _is_login_html(body):
            return None, ""            # not logged in -> stub only
        return body, ""
    if target.kind == "sharepoint":
        if "text/html" in ct:
            return None, ""            # login/consent page, not the file
        return body, ""
    # web article: strip to text (may be a paywall preview - still useful)
    if _is_login_html(body):
        return None, ""
    return None, _html_to_text(body)


def dest_path(root: Path, target: LinkTarget) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', "_", target.title).strip() or "link"
    if target.kind.startswith("google") or target.kind == "sharepoint":
        # SharePoint titles often already carry the extension ("Syllabus.docx");
        # don't double it.
        if safe.lower().endswith("." + target.ext.lower()):
            return root / "_synced" / "links" / safe
        return root / "_synced" / "links" / f"{safe}.{target.ext}"
    return root / "_synced" / "links" / f"{safe}.md"


def note_markdown(target: LinkTarget, extracted: str = "") -> str:
    lines = [
        f"# {target.title}",
        "",
        f"> Link from {target.course} on OAKS"
        + (f" ({target.module})" if target.module else "") + ".",
        f"> Source: {target.url}",
        "",
    ]
    if extracted:
        lines.append(extracted)
    elif target.kind == "reference":
        lines.append("(Video or interactive link - open the URL above; no text "
                     "to index.)")
    else:
        lines.append("(Content not extractable without logging in - open the "
                     "URL above.)")
    return "\n".join(lines) + "\n"
