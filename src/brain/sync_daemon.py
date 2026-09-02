"""Background assignment-sync poller for `brain serve`.

A daemon thread runs the credentialed sync as a DRY RUN on an interval and
caches the findings in memory (and on disk, so a restart still shows the last
result). The CALENDAR is never applied automatically: surfacing "3 new
deadlines on OAKS" is safe and useful; rewriting the calendar without the user
looking is not. The web app exposes the cached status at /api/sync/status and
can trigger an immediate poll at /api/sync/run; applying calendar changes stays
a separate, explicit action.

The one thing the poll DOES write is newly-released course materials: files and
link documents that appear as a course drip-releases content week by week
(_pull_new_content, throttled). That is purely additive - new materials land in
their collections and get indexed, nothing existing is touched - and matches
the user's standing intent to capture everything, so it does not need the same
hands-off treatment as the calendar.

Design notes:
- Own sqlite connection per poll (this runs off the request threads).
- Every failure is captured into the status, never raised - a background
  thread that dies takes the feature down silently, which is worse than a
  visible "session expired" line.
- The loop wakes every few seconds to check a stop event, so shutdown is
  prompt even with a 6-hour interval.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
from dataclasses import dataclass, field
from pathlib import Path

from . import sync as syncmod
from .errors import IndexBusy

# How often the background poll also captures newly-released course materials.
CONTENT_PULL_INTERVAL_S = 6 * 3600

# notified.json remembers what has already been toasted so a restart does not
# re-announce old findings. It is pruned on every write: a semester's worth of
# keys is plenty, and the hard cap bounds the file no matter what.
NOTIFIED_KEEP_DAYS = 120
NOTIFIED_MAX_KEYS = 2000


def _write_atomic(p: Path, text: str) -> None:
    """tmp + os.replace so a reader (or an abrupt process death) can never
    observe a half-written state file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _state_path(data_dir) -> Path:
    return Path(data_dir) / "sync_state.json"


def _now() -> float:
    # Wrapped so tests can monkeypatch; time.time is fine at runtime.
    return time.time()


@dataclass
class SyncStatus:
    last_run: float | None = None          # epoch seconds
    running: bool = False
    # Monotonic per-process counter, bumped once per completed poll. last_run
    # is a wall clock and is NOT a safe termination signal for a watcher: two
    # polls inside the same clock tick can carry the same value, and a system
    # clock that steps backwards makes it go down. run_id only ever increases,
    # so "my run finished" is exactly "run_id changed".
    run_id: int = 0
    ok: bool = True
    sites: list = field(default_factory=list)   # per-site summary dicts
    new_items: list = field(default_factory=list)   # flattened new/moved
    announcements: list = field(default_factory=list)  # unread course news
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "last_run": self.last_run,
            "running": self.running,
            "run_id": self.run_id,
            "ok": self.ok,
            "sites": self.sites,
            "new_items": self.new_items,
            "announcements": self.announcements,
            "total_new": sum(s.get("new", 0) for s in self.sites),
            "total_moved": sum(s.get("moved", 0) for s in self.sites),
            "total_announcements": len(self.announcements),
            "error": self.error,
        }


def _report_to_status(report, prev: SyncStatus) -> SyncStatus:
    sites, new_items = [], []
    for s in report.sites:
        if s.ok and s.recon is not None:
            sites.append({"site": s.site, "label": s.label, "ok": True,
                          "new": len(s.recon.new), "moved": len(s.recon.moved),
                          # Emitted on both branches so every site row has the
                          # same shape; a site that just answered is connected
                          # by definition.
                          "configured": True,
                          "session_age_h": s.session_age_h})
            for c in s.recon.new:
                new_items.append({"site": s.site, "course": c.item.course,
                                  "title": c.item.title, "date": c.item.date,
                                  "time": c.item.start_time or "", "kind": "new"})
            for c in s.recon.moved:
                # old_time carries a RETIME (same day, new hour) - without it
                # the item reads as a "moved" whose date never changed.
                new_items.append({"site": s.site, "course": c.item.course,
                                  "title": c.item.title, "date": c.item.date,
                                  "time": c.item.start_time or "",
                                  "old_date": c.old_date,
                                  "old_time": c.old_time or "", "kind": "moved"})
        else:
            sites.append({"site": s.site, "label": s.label, "ok": False,
                          "new": 0, "moved": 0,
                          # Never-connected sites are reported separately from
                          # broken ones so the UI can offer setup instead of a
                          # warning the user cannot act on.
                          "configured": getattr(s, "configured", True),
                          "error": (s.error or "").splitlines()[0] if s.error else "unknown"})
    # A poll in which EVERY site failed is not "ok": reporting ok=True there
    # made a completely dead sync (all sessions expired) look healthy on the
    # dashboard. Partial failures stay ok - they are visible per-site.
    ok = any(s["ok"] for s in sites) if sites else True
    error = ""
    if sites and not ok:
        error = (f"all {len(sites)} site(s) failed - the saved sessions may "
                 f"need refreshing")
    return SyncStatus(last_run=_now(), running=False, ok=ok,
                      sites=sites, new_items=new_items, error=error,
                      run_id=prev.run_id + 1)


class SyncPoller:
    """Owns the daemon thread and the cached status."""

    def __init__(self, core, interval_minutes: int):
        self.core = core
        self.interval = max(0, int(interval_minutes)) * 60
        self.status = self._load_cached()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Serializes whole polls: the timer thread, /api/sync/run, and
        # /api/sync/apply can all call poll_once concurrently, and two
        # overlapping credentialed scrapes (plus racing grades refreshes)
        # help nobody.
        self._poll_lock = threading.Lock()
        # Content capture (new course files + link docs) is throttled
        # separately from the calendar poll: materials unlock weekly, so
        # pulling them every few hours is plenty, and file downloads are
        # heavier than the calendar dry run. 0.0 lets the first poll run it.
        self._last_content_pull = 0.0

    # ---- persistence --------------------------------------------------

    def _load_cached(self) -> SyncStatus:
        p = _state_path(self.core.config.settings.data_dir)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return SyncStatus(last_run=d.get("last_run"), running=False,
                                  ok=d.get("ok", True), sites=d.get("sites", []),
                                  new_items=d.get("new_items", []),
                                  announcements=d.get("announcements", []),
                                  error=d.get("error", ""),
                                  run_id=int(d.get("run_id", 0)))
            except Exception:
                pass
        return SyncStatus()

    def _save(self) -> None:
        p = _state_path(self.core.config.settings.data_dir)
        try:
            _write_atomic(p, json.dumps(self.status.to_dict(), indent=2))
        except OSError:
            pass

    # ---- one poll -----------------------------------------------------

    def _pull_new_content(self) -> None:
        """Download newly-released course files and link documents into their
        collections and reindex, throttled to CONTENT_PULL_INTERVAL_S.

        Courses like FINC313 drip-release content week by week (each module has
        a 'Begins' date), so materials appear over the term. Both pulls are
        idempotent - a file/doc already present is skipped - so this is safe to
        run every poll. Files download server-side here; auth-gated
        Google/SharePoint links are queued into links_pending for the browser
        extension to fetch on its own alarm. Best-effort: never raises."""
        now = time.monotonic()
        if now - self._last_content_pull < CONTENT_PULL_INTERVAL_S:
            return
        self._last_content_pull = now
        changed: set[str] = set()
        try:
            fr = syncmod.pull_files(self.core.config, apply=True)
            # bytes>0 marks a real new download (dry-run placeholders are 0).
            changed |= {f.course for f in fr.files
                        if f.status == "downloaded" and f.bytes}
        except Exception:
            pass
        try:
            # Quiz body text (a posted study guide / format note lives ONLY
            # in the quiz record). Skips the (common) all-empty case.
            qr = syncmod.pull_quiz_content(self.core.config, apply=True)
            changed |= {q["course"] for q in qr.quizzes
                        if q["status"] in ("new", "updated")}
        except Exception:
            pass
        try:
            # Populates the browser-fetch queue for new Google/SharePoint link
            # topics; the extension drains and reindexes those itself. Web/OAKS
            # links that fetch server-side land as notes we index below.
            syncmod.pull_links(self.core.config, apply=True)
        except Exception:
            pass
        if changed:
            try:
                # wait=False: if the student is already reindexing from the UI,
                # defer to the next poll rather than queueing a second run
                # behind it. New materials are not urgent, and blocking here
                # would hold the poll lock for the length of an index.
                self.core.index(only=sorted(changed), wait=False)
            except IndexBusy:
                pass
            except Exception:
                pass

    def poll_once(self) -> SyncStatus:
        """Run a dry-run sync now and update cached status. Never raises.
        Whole-poll serialization: a caller that arrives while another poll is
        in flight waits for it rather than launching a second scrape."""
        with self._poll_lock:
            return self._poll_once_locked()

    def _poll_once_locked(self) -> SyncStatus:
        with self._lock:
            self.status.running = True
        conn = None
        try:
            conn = self.core.open_db()
            report = syncmod.run(self.core.config, conn, apply=False)
            new_status = _report_to_status(report, self.status)
            # Unread announcements ride along; failures there must not sink
            # the deadline check.
            try:
                news = syncmod.check_news(self.core.config, apply=False)
                new_status.announcements = [
                    {"course": n["course"], "title": n["title"],
                     "date": n["date"], "id": n["id"]}
                    for n in news.new
                ]
            except Exception:
                new_status.announcements = self.status.announcements
            # Grades cache refresh (best-effort; chat's grade digest reads it).
            try:
                from . import grades as grades_mod

                grades_mod.refresh(self.core.config)
            except Exception:
                pass
            # Capture newly-released course materials (throttled). Unlike the
            # calendar this DOES write, but only additively: new files/link
            # docs land in their collections and get indexed - exactly what the
            # user would download by hand. Nothing existing is rewritten.
            self._pull_new_content()
            self._notify_new(new_status)
        except Exception as e:
            # A failed poll is still a FINISHED poll. If run_id did not tick
            # here, a caller watching for its run to end would wait forever on
            # exactly the runs it most needs to hear about.
            new_status = SyncStatus(last_run=_now(), running=False, ok=False,
                                    sites=self.status.sites,
                                    new_items=self.status.new_items,
                                    announcements=self.status.announcements,
                                    error=f"{type(e).__name__}: {e}",
                                    run_id=self.status.run_id + 1)
        finally:
            if conn is not None:
                conn.close()
        with self._lock:
            self.status = new_status
            self._save()
        return new_status

    # ---- notifications -------------------------------------------------

    def _notified_path(self) -> Path:
        return Path(self.core.config.settings.data_dir) / "notified.json"

    def _load_notified(self) -> dict:
        """key -> ISO date it was first announced. Accepts the legacy plain
        list (undated) by treating those keys as seen today, so upgrading
        never re-toasts a backlog."""
        try:
            raw = json.loads(self._notified_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
        today = date.today().isoformat()
        if isinstance(raw, list):
            return {str(k): today for k in raw}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        return {}

    @staticmethod
    def _prune_notified(seen: dict) -> dict:
        """Drop keys older than NOTIFIED_KEEP_DAYS, then hard-cap to the most
        recent NOTIFIED_MAX_KEYS. Without this the file grew forever."""
        cutoff = (date.today() - timedelta(days=NOTIFIED_KEEP_DAYS)).isoformat()
        kept = {k: v for k, v in seen.items() if v >= cutoff}
        if len(kept) > NOTIFIED_MAX_KEYS:
            newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)
            kept = dict(newest[:NOTIFIED_MAX_KEYS])
        return kept

    def _notify_new(self, status: SyncStatus) -> None:
        """One Windows toast per poll covering everything not yet announced.
        A key set on disk keeps restarts from re-toasting old findings."""
        seen = self._load_notified()
        # The time is part of the key: a quiz retimed twice on the same day
        # must toast twice, not fall silent after the first.
        def _ikey(i):
            return (f"i|{i.get('site')}|{i.get('course')}|{i.get('title')}"
                    f"|{i.get('date')}|{i.get('time', '')}")

        fresh_items = [i for i in status.new_items if _ikey(i) not in seen]
        fresh_news = [a for a in status.announcements
                      if f"a|{a.get('id')}" not in seen]
        if not fresh_items and not fresh_news:
            return
        parts = []
        if fresh_items:
            parts.append(f"{len(fresh_items)} new/moved assignment(s)")
        if fresh_news:
            parts.append(f"{len(fresh_news)} announcement(s)")
        first = (fresh_items or fresh_news)[0]
        detail = f"{first.get('course', '')}: {first.get('title', '')}"[:120]
        from . import notify

        if not notify.toast("Command Center - " + ", ".join(parts), detail):
            # Surface the failure instead of silently disabling the feature
            # (e.g. WDAC refusing the temp .ps1): the status line makes it
            # visible in /api/sync/status and the pip popover.
            status.error = (status.error + " · " if status.error else "") \
                + "toast notification failed"
        today = date.today().isoformat()
        for i in fresh_items:
            seen[_ikey(i)] = today
        for a in fresh_news:
            seen[f"a|{a.get('id')}"] = today
        try:
            _write_atomic(self._notified_path(),
                          json.dumps(self._prune_notified(seen), indent=1,
                                     sort_keys=True))
        except OSError:
            pass

    # ---- daemon loop --------------------------------------------------

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sync-poller",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # A short grace period so startup isn't competing with the first page
        # load, then poll on the interval, waking often to honor stop().
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            self.poll_once()
            # wait() returns True if stopped; sleep in <=5s slices
            waited = 0
            while waited < self.interval and not self._stop.is_set():
                if self._stop.wait(min(5, self.interval - waited)):
                    return
                waited += 5

    def stop(self) -> None:
        self._stop.set()
