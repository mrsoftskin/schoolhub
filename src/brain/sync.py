"""Orchestrate credentialed assignment sync across the four platforms.

Flow: for each site with a stored session, pull items, reconcile them against
what the calendar already holds, and report new/moved items. With apply=True,
new items and date-moves are written into fixed.csv (as source-tagged rows)
and the calendar is reimported. Nothing is written on a dry run, and a site
whose session is missing/expired is reported, never silently skipped.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .connectors import ExistingEvent, LoginRequired, SessionStore, get, REGISTRY
from .connectors.detect import Reconciliation, reconcile


@dataclass
class SiteResult:
    site: str
    label: str
    ok: bool
    recon: Reconciliation | None = None
    error: str = ""
    session_age_h: float | None = None
    # False when this site has no saved session at all. A site the student
    # never connected is NOT a broken site, and conflating the two is why a
    # friend who only uses OAKS saw three permanent orange warnings naming
    # services they have never heard of. The sync runs every connector in
    # REGISTRY, so every unused one used to report itself as a failure.
    configured: bool = True


@dataclass
class SyncReport:
    sites: list[SiteResult] = field(default_factory=list)
    applied: int = 0

    @property
    def total_new(self) -> int:
        return sum(len(s.recon.new) for s in self.sites if s.recon)

    @property
    def total_moved(self) -> int:
        return sum(len(s.recon.moved) for s in self.sites if s.recon)


def _active_sites(config: Config, only: str | None) -> list[str]:
    """Site names this run should touch.

    An explicit `only` always wins, so a disabled site stays testable with
    `brain sync --site <name>`; otherwise settings.sync_sites_off is removed.
    """
    if only:
        return [only]
    # getattr, not attribute access: several call sites build a minimal
    # settings stub, and a missing field should mean "nothing disabled"
    # rather than crashing the whole sync.
    off = set(getattr(config.settings, "sync_sites_off", None) or [])
    return [n for n in REGISTRY if n not in off]


def _existing(conn) -> list[ExistingEvent]:
    rows = conn.execute(
        "SELECT course, title, starts_at, all_day, source FROM events").fetchall()
    # An all-day row is stored at midnight, but that 00:00 is a placeholder,
    # not a time the user set. Reporting it as a stored time would make every
    # timed platform item look like a retime of it.
    return [ExistingEvent(course=r["course"], title=r["title"],
                          date=r["starts_at"][:10], source=r["source"],
                          start_time=("" if r["all_day"]
                                      else r["starts_at"][11:16]))
            for r in rows]


def _load_ignore(config: Config) -> set[str]:
    """Normalized keys of items the user has deliberately suppressed.

    Deleting a bogus platform event from fixed.csv does not stop the source
    from serving it - sync would re-add it forever. Listing its key in
    calendar/sync_ignore.txt (one per line, # comments allowed) drops it at
    pull time instead.
    """
    if not config.calendar or not config.calendar.fixed_csv:
        return set()
    p = config.calendar.fixed_csv.parent / "sync_ignore.txt"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def run(config: Config, conn, *, only: str | None = None, apply: bool = False) -> SyncReport:
    from .connectors.detect import _key

    from datetime import timedelta

    store = SessionStore(config.settings.data_dir)
    courses = config.collection_names()
    existing = _existing(conn)
    ignore = _load_ignore(config)
    report = SyncReport()
    names = _active_sites(config, only)

    # Copied course shells carry placeholder dates for other terms; nothing
    # past semester end (plus a grace week or two for finals spillover) is a
    # real deadline this term.
    window_hi = None
    if config.calendar:
        window_hi = config.calendar.semester_end + timedelta(days=14)

    to_write: list = []          # Change objects, so apply knows old date/time
    for name in names:
        conn_obj = get(name)
        res = SiteResult(site=name, label=conn_obj.label, ok=False,
                         session_age_h=store.age_hours(name))
        if not store.has(name):
            # Not connected, rather than failing. The message stays useful on
            # the CLI, and `configured` lets the UI say "connect this" instead
            # of "your saved logins need refreshing".
            res.configured = False
            res.error = f"not connected (run: brain sync login {name})"
            report.sites.append(res)
            continue
        try:
            items = conn_obj.pull(store.load(name), courses, window_hi=window_hi)
        except LoginRequired as e:
            res.error = str(e)
            report.sites.append(res)
            continue
        except Exception as e:  # unexpected; report, do not crash the run
            res.error = f"{type(e).__name__}: {e}"
            report.sites.append(res)
            continue
        res.ok = True
        res.recon = reconcile(items, existing, today=date.today().isoformat())
        # sync_ignore suppresses re-ADDS, and only those. Filtering at pull
        # time also hid moves/retimes of rows that legitimately exist: the
        # key written to mute a stale "Final Exam Spring 2026" shell event is
        # the same key as the real final's quiz record, so a date change on
        # the term's highest-stakes deadline would never have surfaced.
        if ignore:
            # Suppress BOTH new and moved. Filtering only re-adds left a
            # muted item still able to reach the destructive apply path as a
            # "moved" change and rewrite a real deadline's time, with no way
            # for the user to stop it - the opposite of what muting means.
            res.recon.new = [c for c in res.recon.new
                             if _key(c.item.course, c.item.title) not in ignore]
            res.recon.moved = [c for c in res.recon.moved
                               if _key(c.item.course, c.item.title) not in ignore]
        report.sites.append(res)
        if apply:
            to_write.extend(res.recon.new)
            to_write.extend(res.recon.moved)

    if apply and to_write and config.calendar and config.calendar.fixed_csv:
        report.applied = _apply_changes(config.calendar.fixed_csv, to_write)
    return report


_COLS = ["course", "title", "date", "start_time", "end_time", "all_day", "kind"]


@dataclass
class FileResult:
    course: str
    filename: str
    module_path: str
    dest: str
    status: str          # "downloaded" | "skipped" (already have it) | "failed"
    bytes: int = 0
    error: str = ""


@dataclass
class FilesReport:
    files: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # (site, message)

    @property
    def downloaded(self) -> int:
        return sum(1 for f in self.files if f.status == "downloaded")

    @property
    def skipped(self) -> int:
        return sum(1 for f in self.files if f.status == "skipped")


def _collection_root(config: Config, course: str):
    """First existing root path for a collection, or None."""
    from pathlib import Path

    for col in config.collections:
        if col.name == course:
            for r in col.roots:
                p = Path(r)
                if p.exists():
                    return p
            return Path(col.roots[0]) if col.roots else None
    return None


def pull_files(config: Config, *, only: str | None = None,
               apply: bool = False, subdir: str = "_synced") -> FilesReport:
    """Pull newly-uploaded course files from each file-capable site into the
    matching collection folder. Only OAKS exposes course files; others are
    skipped. A file already present anywhere under the collection root (by
    name) is not re-downloaded, so a file the user already has by hand or from
    the original export is left alone. With apply=False nothing is written.
    """
    store = SessionStore(config.settings.data_dir)
    report = FilesReport()
    names = _active_sites(config, only)
    for name in names:
        conn_obj = get(name)
        lister = getattr(conn_obj, "list_files", None)
        if lister is None:
            continue                       # site has no file feed
        if not store.has(name):
            report.errors.append((name, f"no saved session (brain sync login {name})"))
            continue
        try:
            session = store.load(name)
            listing = lister(session, config.collection_names())
        except LoginRequired as e:
            report.errors.append((name, str(e)))
            continue
        except Exception as e:
            report.errors.append((name, f"{type(e).__name__}: {e}"))
            continue

        for f in listing:
            root = _collection_root(config, f["course"])
            if root is None:
                continue
            # Already have a file by this name anywhere in the collection?
            existing = next(root.rglob(f["filename"]), None) if root.exists() else None
            dest = root / subdir / f["filename"]
            if existing is not None or dest.exists():
                report.files.append(FileResult(
                    course=f["course"], filename=f["filename"],
                    module_path=f["module_path"], dest=str(existing or dest),
                    status="skipped"))
                continue
            if not apply:
                report.files.append(FileResult(
                    course=f["course"], filename=f["filename"],
                    module_path=f["module_path"], dest=str(dest),
                    status="downloaded", bytes=0))   # would download
                continue
            try:
                n = conn_obj.download_file(session, f["ou"], f["topic_id"], dest)
                report.files.append(FileResult(
                    course=f["course"], filename=f["filename"],
                    module_path=f["module_path"], dest=str(dest),
                    status="downloaded", bytes=n))
            except Exception as e:
                report.files.append(FileResult(
                    course=f["course"], filename=f["filename"],
                    module_path=f["module_path"], dest=str(dest),
                    status="failed", error=str(e)))
    return report


@dataclass
class LinkResult:
    course: str
    title: str
    kind: str
    status: str          # "content" | "note" | "skipped" | "failed"
    dest: str = ""
    error: str = ""


@dataclass
class LinksReport:
    links: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def with_content(self) -> int:
        return sum(1 for l in self.links if l.status == "content")

    @property
    def notes(self) -> int:
        return sum(1 for l in self.links if l.status in ("note", "content"))


def pull_links(config: Config, *, apply: bool = False) -> LinksReport:
    """Turn every OAKS Link topic into a searchable note; embed real content
    for Google/SharePoint/web links whose session is available. Nothing is
    skipped - a link with no reachable content still gets a stub note."""
    from . import links as linkmod

    store = SessionStore(config.settings.data_dir)
    report = LinksReport()
    if not store.has("oaks"):
        report.errors.append(("oaks", "no saved session (brain sync login oaks)"))
        return report
    conn_obj = get("oaks")
    lister = getattr(conn_obj, "list_links", None)
    if lister is None:
        return report
    try:
        raw = lister(store.load("oaks"), config.collection_names())
    except LoginRequired as e:
        report.errors.append(("oaks", str(e).splitlines()[0]))
        return report
    except Exception as e:
        report.errors.append(("oaks", f"{type(e).__name__}: {e}"))
        return report

    sessions: dict = {}
    for name in ("google", "sharepoint", "oaks", "blended"):
        try:
            sessions[name] = store.load(name) if store.has(name) else None
        except Exception:
            sessions[name] = None

    pending: list[dict] = []       # auth-gated links for the browser extension
    for item in raw:
        t = linkmod.classify(item["course"], item["title"], item["url"],
                             item.get("module_path", ""))
        root = _collection_root(config, t.course)
        if root is None:
            continue
        dest = linkmod.dest_path(root, t)
        if not apply:
            report.links.append(LinkResult(t.course, t.title, t.kind,
                                           "content" if t.session else "note",
                                           str(dest)))
            continue
        # Idempotent re-runs: a Google/SharePoint doc the browser already
        # imported lands as the real file at `dest` (the stub .md is removed on
        # import). Re-fetching would fail server-side, rewrite a stub, and
        # re-queue it - making the extension download it again every poll. So
        # once the real doc is present, leave it. (A doc edited upstream won't
        # refresh automatically; a manual `brain sync links --apply` after
        # deleting the file forces it. Matches pull_files' skip-if-present rule.)
        if t.session in ("google", "sharepoint") and dest.exists():
            report.links.append(LinkResult(t.course, t.title, t.kind,
                                           "content", str(dest)))
            continue
        session = sessions.get(t.session) if t.session else None
        try:
            body, extracted = linkmod.fetch_content(t, session)
        except Exception:
            # A transient fetch failure must not drop an auth-gated link from
            # the browser-fetch queue; fall through so it still gets a stub and
            # a pending entry below (the browser will fetch it authenticated).
            body, extracted = None, ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if body is not None:
            dest.write_bytes(body)
            # Replace any placeholder stub from an earlier session-less run.
            stub = dest.with_suffix(".md")
            if stub != dest and stub.exists():
                stub.unlink()
            report.links.append(LinkResult(t.course, t.title, t.kind, "content",
                                           str(dest)))
        else:
            note = linkmod.dest_path(root, t)
            if t.session:                    # google/sharepoint dest had a doc ext
                note = note.with_suffix(".md")
            note.write_text(linkmod.note_markdown(t, extracted), encoding="utf-8")
            report.links.append(LinkResult(
                t.course, t.title, t.kind,
                "content" if extracted else "note", str(note)))
            # Only Google/SharePoint truly need the browser (server-side is
            # blocked by anti-scraping). OAKS/Blended pages fetch fine here, so
            # they stay stubs on failure rather than joining the browser queue.
            if t.session in ("google", "sharepoint") and not extracted:
                pending.append({
                    "id": linkmod.link_id(t.url),
                    "course": t.course, "title": t.title, "kind": t.kind,
                    "url": t.url, "fetch_url": linkmod.browser_fetch_url(t),
                    "ext": t.ext, "dest": str(dest),
                })

    if apply:
        _write_links_pending(config, pending)
    return report


def _links_pending_path(config: Config):
    from pathlib import Path

    return Path(config.settings.data_dir) / "links_pending.json"


def _write_links_pending(config: Config, pending: list) -> None:
    import json as _json

    p = _links_pending_path(config)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(pending, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_links_pending(config: Config) -> list:
    import json as _json

    p = _links_pending_path(config)
    if not p.exists():
        return []
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def import_downloaded_link(config: Config, link_id: str,
                           downloads_dir=None) -> dict:
    """Move a chrome.downloads-fetched doc from Downloads/cc-links/<id>.<ext>
    into its collection and mark it done. Raises FileNotFoundError if the
    download hasn't landed, ValueError for an unknown id."""
    from pathlib import Path

    pending = load_links_pending(config)
    entry = next((e for e in pending if e.get("id") == link_id), None)
    if entry is None:
        raise ValueError(f"unknown link id {link_id!r}")
    dl = Path(downloads_dir) if downloads_dir else Path.home() / "Downloads"
    src = dl / "cc-links" / f"{link_id}.{entry.get('ext', 'txt')}"
    if not src.exists():
        raise FileNotFoundError(str(src))
    content = src.read_bytes()
    from . import links as linkmod

    if linkmod._is_login_html(content[:2000]):
        raise ValueError("downloaded a login page, not the document")
    dest = Path(entry["dest"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    stub = dest.with_suffix(".md")
    if stub != dest and stub.exists():
        stub.unlink()
    try:
        src.unlink()
    except OSError:
        pass
    _write_links_pending(config, [e for e in pending if e.get("id") != link_id])
    return {"ok": True, "course": entry["course"], "dest": str(dest)}


def save_browser_fetched(config: Config, link_id: str, content: bytes) -> dict:
    """Save content the extension fetched in-browser, matched by link id to a
    pending entry. Returns {ok, course, dest} or raises ValueError."""
    from pathlib import Path

    pending = load_links_pending(config)
    entry = next((e for e in pending if e.get("id") == link_id), None)
    if entry is None:
        raise ValueError(f"unknown link id {link_id!r}")
    # Reject a login/consent page masquerading as the file.
    from . import links as linkmod

    if linkmod._is_login_html(content[:2000]):
        raise ValueError("looks like a login page, not the document")
    dest = Path(entry["dest"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    stub = dest.with_suffix(".md")
    if stub != dest and stub.exists():
        stub.unlink()
    # Drop it from pending.
    remaining = [e for e in pending if e.get("id") != link_id]
    _write_links_pending(config, remaining)
    return {"ok": True, "course": entry["course"], "dest": str(dest)}


@dataclass
class NewsReport:
    new: list = field(default_factory=list)      # announcement dicts not yet seen
    total: int = 0
    saved: int = 0
    errors: list = field(default_factory=list)   # (site, message)


def _seen_news_path(config: Config):
    from pathlib import Path

    return Path(config.settings.data_dir) / "announcements_seen.json"


def _slug(text: str, limit: int = 48) -> str:
    import re as _re

    s = _re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:limit].rstrip("-") or "announcement"


def check_news(config: Config, *, apply: bool = False) -> NewsReport:
    """New (unseen) course announcements across news-capable sites.

    Dry run reports them and does NOT mark them seen - like unread mail, they
    stay "new" until applied. apply=True writes each as a small Markdown file
    under <course_root>/_synced/announcements/ (so it indexes and Chat can
    cite it) and marks it seen. Never deletes anything.
    """
    import json as json_mod

    store = SessionStore(config.settings.data_dir)
    report = NewsReport()
    seen_path = _seen_news_path(config)
    try:
        seen = set(json_mod.loads(seen_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()

    for name in _active_sites(config, None):
        conn_obj = get(name)
        lister = getattr(conn_obj, "list_news", None)
        if lister is None or not store.has(name):
            continue
        try:
            items = lister(store.load(name), config.collection_names())
        except LoginRequired as e:
            report.errors.append((name, str(e).splitlines()[0]))
            continue
        except Exception as e:
            report.errors.append((name, f"{type(e).__name__}: {e}"))
            continue
        report.total += len(items)
        for n in items:
            if n["id"] in seen:
                continue
            report.new.append(n)
            if not apply:
                continue
            root = _collection_root(config, n["course"])
            if root is None:
                continue
            dest = (root / "_synced" / "announcements"
                    / f"{n['date']} {_slug(n['title'])}.md")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                f"# {n['title']}\n\n"
                f"> Announcement posted on {conn_obj.label} for {n['course']} "
                f"on {n['date']}.\n\n{n['text']}\n",
                encoding="utf-8")
            seen.add(n["id"])
            report.saved += 1

    if apply and report.saved:
        try:
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            seen_path.write_text(json_mod.dumps(sorted(seen)), encoding="utf-8")
        except OSError:
            pass
    return report


@dataclass
class QuizContentReport:
    quizzes: list = field(default_factory=list)  # {course,name,status,dest}
    total: int = 0                               # quizzes checked
    saved: int = 0                               # files written (apply only)
    errors: list = field(default_factory=list)   # (site, message)


def pull_quiz_content(config: Config, *, apply: bool = False) -> QuizContentReport:
    """Save quiz body text (Description/Instructions/Header/Footer) as
    Markdown under <course_root>/_synced/quizzes/, so "did he post anything
    about the quiz?" is answerable from Chat. The quiz record is the ONLY
    place this text lives - no other endpoint exposes it. Most quizzes carry
    none (verified live: all 14 FINC313 bodies empty), so only quizzes with
    real text get a file; a file is refreshed when the text changes upstream.
    """
    store = SessionStore(config.settings.data_dir)
    report = QuizContentReport()
    if not store.has("oaks"):
        report.errors.append(("oaks", "no saved session (brain sync login oaks)"))
        return report
    conn_obj = get("oaks")
    lister = getattr(conn_obj, "list_quiz_content", None)
    if lister is None:
        return report
    try:
        listing = lister(store.load("oaks"), config.collection_names())
    except LoginRequired as e:
        report.errors.append(("oaks", str(e).splitlines()[0]))
        return report
    except Exception as e:
        report.errors.append(("oaks", f"{type(e).__name__}: {e}"))
        return report

    seen_slugs: set = set()
    for q in listing:
        if q.get("error"):
            report.errors.append(("oaks", f"{q.get('course')}: {q['error']}"))
            continue
        report.total += 1
        body = "\n\n".join(
            t for t in (q.get("description"), q.get("instructions"),
                        q.get("header"), q.get("footer")) if t)
        if not body:
            continue
        root = _collection_root(config, q["course"])
        if root is None:
            continue
        meta = " - ".join(x for x in (
            f"due {q['due']}" if q.get("due") else "",
            q.get("attempts") or "") if x)
        text = (f"# {q['name']}\n\n"
                f"> Quiz on OAKS for {q['course']}"
                + (f" ({meta})" if meta else "") + ".\n\n"
                + body + "\n")
        # Two quiz names can slugify identically (D2L allows duplicate names,
        # and _slug truncates at 48 chars). Disambiguate the collision with
        # the quiz id so they never overwrite each other in a loop; the first
        # of a name keeps the plain slug, so existing files do not churn.
        slug = _slug(q["name"])
        if (q["course"], slug) in seen_slugs:
            slug = f"{slug}-{str(q.get('id') or '').rsplit('-', 1)[-1]}"
        seen_slugs.add((q["course"], _slug(q["name"])))
        dest = root / "_synced" / "quizzes" / f"{slug}.md"
        try:
            current = dest.read_text(encoding="utf-8") if dest.exists() else None
        except OSError:
            current = None
        if current == text:
            status = "unchanged"
        else:
            status = "new" if current is None else "updated"
        report.quizzes.append({"course": q["course"], "name": q["name"],
                               "status": status, "dest": str(dest)})
        if status == "unchanged" or not apply:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        report.saved += 1
    return report


def _apply_changes(csv_path, changes) -> int:
    """Write reconciled changes into fixed.csv: a MOVED item (date or time)
    rewrites its existing row in place - appending the new date used to leave
    the stale row behind as a second, wrong deadline - and NEW items append.

    This is the only code that EDITS rather than appends to the calendar's
    source of truth, so it is deliberately timid: it rewrites only when
    exactly one row matches, keeps every field the source does not speak to
    (title, kind, and a hand-set end time), takes a dated backup before the
    first rewrite, and fsyncs before the atomic replace. Anything ambiguous
    is left alone and stays reported rather than guessed at.
    Returns the number of rows written.
    """
    import os
    import shutil

    from .connectors.detect import _key, same_deadline

    header, rows = list(_COLS), []
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            raw = [row for row in csv.reader(f) if row]
        if raw and (raw[0][0] or "").strip().lower() == "course":
            header, rows = raw[0], raw[1:]
        else:
            rows = raw
    except FileNotFoundError:
        pass

    def _slot_matches(row, c) -> bool:
        """Same course, same old date, and a stored time the change knows."""
        it = c.item
        if len(row) < 7 or row[0] != it.course or row[2] != (c.old_date or ""):
            return False
        # zfill: a hand-typed "9:00" must match the stored "09:00". An empty
        # stored time matches only when no time was recorded for the slot.
        stored = row[3].strip()
        want = tuple(c.old_times) or ((c.old_time,) if c.old_time else ())
        if want and (stored.zfill(5) if stored else "") not in want:
            return False
        return True

    def _candidates(c) -> list:
        """Rows this change could rewrite, strongest match first.

        TIERED on purpose. same_deadline() is a recall-tuned heuristic - it
        exists so a reworded title is not reported as brand-new, and it will
        match two DIFFERENT assignments sharing a course, a date and one
        word. Tolerable when the cost is a missing "new" line; not when the
        cost is overwriting an unrelated deadline. So an EXACT key match
        always wins, and the loose match is used only when it is the sole
        candidate - which is what still lets "Chapter 2 Assignment" rewrite
        the stored "Connect: Chapter 2" instead of appending a duplicate.
        """
        it = c.item
        slot = [r for r in rows if _slot_matches(r, c)]
        exact = [r for r in slot if _key(r[0], r[1]) == _key(it.course, it.title)]
        if exact:
            return exact
        return [r for r in slot
                if same_deadline(r[0], r[1], it.course, it.title)]

    rewritten = 0
    to_append = []
    ambiguous = 0
    for c in changes:
        it = c.item
        if getattr(c, "kind", "new") != "moved":
            to_append.append(it)
            continue
        hits = _candidates(c)
        if len(hits) > 1:
            # Two rows claim the same deadline (duplicates from the old
            # append-on-move behavior, or two genuinely similar titles).
            # Editing one and stranding the other would be worse than
            # reporting it, so touch neither.
            ambiguous += 1
            continue
        if not hits:
            is_retime = c.old_time is not None and c.old_date == (it.date or "")
            # This one may stay loose: it only SUPPRESSES an append, so a
            # false positive costs a missing row, not a destroyed one.
            already = any(row[0] == it.course and row[2] == (it.date or "")
                          and same_deadline(row[0], row[1], it.course, it.title)
                          for row in rows if len(row) >= 7)
            if is_retime or already:
                # The stored occurrence lives somewhere sync cannot edit (an
                # ics feed, a recurring rule) or is already on the calendar
                # under other wording. Appending could only add a same-day
                # duplicate; leave it reported instead.
                continue
            to_append.append(it)
            continue
        target = hits[0]
        new_start = "" if it.all_day else it.start_time
        target[2] = it.date or target[2]
        target[3] = new_start
        # Keep a hand-set end time the source has no opinion about (the
        # in-class quiz window "09:00-10:00" is curated, and quiz records
        # carry no end), but drop it if the new start has passed it.
        # Pad BOTH sides before comparing. Lexically "9:50" > "10:00" is
        # True, so an unpadded stored end survived a move past 10:00 and left
        # end_time BEFORE start_time - a row the calendar importer rejects,
        # which silently downgrades the whole csv source from rebuild to
        # upsert-only, so deletions stop propagating. An Excel round-trip
        # unpads every single-digit hour in the file at once, so this is a
        # foreseeable state, not a typo.
        stored_end = (target[4] or "").strip()
        padded_end = stored_end.zfill(5) if stored_end else ""
        padded_start = new_start.zfill(5) if new_start else ""
        if it.all_day:
            target[4] = ""
        elif it.end_time:
            target[4] = it.end_time
        else:
            target[4] = (padded_end if (padded_start and padded_end
                                        and padded_end > padded_start) else "")
        target[5] = "true" if it.all_day else "false"
        rewritten += 1

    if rewritten:
        # One dated backup per day, taken before the first edit of the day -
        # an Excel resave of this file once wiped every deadline, so an edit
        # path without a rollback is not acceptable here.
        backup = f"{csv_path}.bak-{date.today().isoformat()}"
        if not os.path.exists(backup):
            try:
                shutil.copy2(csv_path, backup)
            except OSError:
                pass
        tmp = f"{csv_path}.tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(header)
                w.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, csv_path)
        except PermissionError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            from .errors import BrainError

            raise BrainError(
                f"Could not update {csv_path} - it is open in another program "
                f"(close it in Excel and run this again). Nothing was changed."
            ) from e
    return rewritten + _append_rows(csv_path, to_append)


def _append_rows(csv_path, items) -> int:
    have = set()
    try:
        for r in csv.DictReader(open(csv_path, encoding="utf-8-sig")):
            have.add((r["course"], r["title"], r["date"]))
    except FileNotFoundError:
        pass
    new_rows, seen = [], set()
    for it in items:
        row = it.csv_row()
        key = (row[0], row[1], row[2])
        if key in have or key in seen or not row[2]:
            continue
        seen.add(key)
        new_rows.append(row)
    if new_rows:
        # A file whose last line has no newline (any hand edit in an editor
        # that does not add one) would otherwise get the first appended row
        # glued onto it, corrupting BOTH rows and dropping a real deadline
        # from the imported calendar.
        needs_nl = False
        try:
            with open(csv_path, "rb") as f:
                f.seek(0, 2)
                if f.tell():
                    f.seek(-1, 2)
                    needs_nl = f.read(1) not in b"\r\n"
        except OSError:
            pass
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            if needs_nl:
                f.write("\n")
            csv.writer(f, lineterminator="\n").writerows(new_rows)
    return len(new_rows)


def _cookie_from_curl(cmd: str) -> str:
    """Pull the Cookie value out of a DevTools "Copy as cURL" command.

    Chrome/Edge offer two flavors: bash (single-quoted args, backslash line
    continuations) and cmd (double-quoted args where ", %, &, | are escaped
    with a caret). The caret de-escape runs only when the blob actually looks
    like the cmd flavor, so a bash cookie containing a literal ^ survives.
    """
    import re as _re
    if "^" in cmd and _re.search(r'\^"', cmd):
        cmd = _re.sub(r"\^(.)", r"\1", cmd)          # ^" -> "  ^% -> %  ^^ -> ^
    for m in _re.finditer(r"""-H\s+(['"])(.*?)\1""", cmd, _re.DOTALL):
        header = m.group(2)
        if _re.match(r"^\s*cookie\s*:", header, _re.IGNORECASE):
            return header
    m = _re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", cmd, _re.DOTALL)
    if m:
        return m.group(2)
    return ""


def parse_cookie_blob(blob: str) -> dict[str, str]:
    """Parse cookies out of whatever the user pasted from DevTools.

    Accepted forms, most-specific first:
      - a "Copy as cURL" command (bash or cmd flavor): the cookie header is
        extracted from its -H/-b arguments;
      - a "Copy request headers" block (many "name: value" lines): only the
        Cookie line is used, so referer URLs with = in them are not misread
        as cookies;
      - a raw `Cookie:` header, with or without the "Cookie:" prefix;
      - a newline/semicolon list of name=value pairs.
    """
    import re as _re
    text = blob.strip()
    if _re.match(r"^\s*curl\s", text, _re.IGNORECASE):
        text = _cookie_from_curl(text)
    elif "\n" in text:
        for line in text.splitlines():
            if _re.match(r"^\s*cookie\s*:", line, _re.IGNORECASE):
                text = line
                break
    cookies: dict[str, str] = {}
    text = _re.sub(r"^\s*cookie\s*:\s*", "", text.strip(), flags=_re.IGNORECASE)
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name and name.lower() != "cookie":
            cookies[name] = value.strip()
    return cookies
