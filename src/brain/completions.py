"""Which assignments the student has finished.

This is the app's only piece of original, non-reconstructible data. Everything
else - the index, the calendar, grades - can be rebuilt from a course site or
from files on disk, and the guides actively tell people to delete `data/` to
repair a broken index. So completions do NOT live there: they live beside
calendar/fixed.csv, in the folder that holds the other curated, hand-editable
state (fixed.csv, sync_ignore.txt).

IDENTITY is the hard part. An event's id is sha1(source|course|title|starts_at),
so it changes the moment sync retimes a deadline - which now happens routinely.
A completion keyed on that id would silently disappear the first time a
professor moved a quiz. Instead a completion names a SLOT:

    (course, detect._key(course, title), due_date)

Measured against the live calendar, all 356 events map to 356 distinct slots -
no collisions - while a retime (12:00 -> 10:00) leaves the slot untouched.
Including the DATE is what keeps a repeating series independent: 42 VHL
homework rows share one normalized title, so without the date one tick would
clear the whole semester.

The file is append-only JSON Lines and the fold is "last line wins per slot",
so un-ticking appends `state: open` rather than deleting anything. That keeps
the history intact and makes a partial write survivable: a truncated final
line is dropped and the previous state stands.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .connectors.detect import _key, _parts, same_deadline

FILENAME = "completions.jsonl"

# Same rule the reconciler uses for VHL: a day-summary ("Supersite: 8
# activities") and the generic recurring row ("VHL Supersite homework") are
# the same obligation, so ticking one must tick the other.
_PLATFORM_WORDS = {"vhl", "supersite"}

_lock = threading.Lock()


def path_for(config) -> Path:
    """Where completions live: beside fixed.csv when a calendar is configured,
    otherwise in the data dir so the feature still works."""
    cal = getattr(config, "calendar", None)
    if cal is not None and getattr(cal, "fixed_csv", None):
        return Path(cal.fixed_csv).parent / FILENAME
    return Path(config.settings.data_dir) / FILENAME


def slot(course: str, title: str, date: str) -> tuple[str, str, str]:
    """The stable identity of one obligation on one day."""
    return (course.upper(), _key(course, title), (date or "")[:10])


@dataclass(frozen=True)
class Completion:
    course: str
    key: str
    date: str
    title: str
    done: bool
    at: str

    @property
    def slot(self) -> tuple[str, str, str]:
        return (self.course, self.key, self.date)


def load(config) -> dict[tuple[str, str, str], Completion]:
    """Fold the log into current state, newest line winning per slot."""
    p = path_for(config)
    out: dict[tuple[str, str, str], Completion] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            # A half-written final line (power loss mid-append) must not take
            # the whole history with it.
            continue
        try:
            c = Completion(
                course=str(rec["course"]).upper(), key=str(rec["key"]),
                date=str(rec["date"])[:10], title=str(rec.get("title") or ""),
                done=bool(rec.get("done", True)), at=str(rec.get("at") or ""),
            )
        except (KeyError, TypeError):
            continue
        out[c.slot] = c
    return out


def set_done(config, *, course: str, title: str, date: str, done: bool,
             now: str | None = None) -> Completion:
    """Record that one obligation is finished (or is not, after all)."""
    course_u, key, day = slot(course, title, date)
    rec = Completion(course=course_u, key=key, date=day, title=title or "",
                     done=bool(done),
                     at=now or datetime.now().isoformat(timespec="seconds"))
    p = path_for(config)
    line = json.dumps({
        "v": 1, "course": rec.course, "key": rec.key, "date": rec.date,
        "title": rec.title, "done": rec.done, "at": rec.at,
    }, ensure_ascii=True)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


def _platform_words(course: str, title: str) -> set[str]:
    _up, content, _nums = _parts(course, title)
    return set(content) & _PLATFORM_WORDS


def resolver(config):
    """Return is_done(course, title, date) -> bool for a batch of events.

    Three passes, cheapest first: the exact slot; then a differently-worded
    twin on the same course and day (the OAKS mirror of a Connect assignment);
    then the platform bucket, which catches the VHL pair whose titles share
    neither key nor numbers.
    """
    state = load(config)
    done = [c for c in state.values() if c.done]
    by_course_date: dict[tuple[str, str], list[Completion]] = {}
    for c in done:
        by_course_date.setdefault((c.course, c.date), []).append(c)

    def is_done(course: str, title: str, date: str) -> bool:
        s = slot(course, title, date)
        hit = state.get(s)
        if hit is not None:
            return hit.done          # an explicit un-tick stops here
        candidates = by_course_date.get((s[0], s[2]))
        if not candidates:
            return False
        for c in candidates:
            if c.title and same_deadline(course, title, c.course, c.title):
                return True
        mine = _platform_words(course, title)
        if mine:
            for c in candidates:
                if c.title and (_platform_words(c.course, c.title) & mine):
                    return True
        return False

    return is_done


def stats(config) -> dict:
    state = load(config)
    return {
        "total": len(state),
        "done": sum(1 for c in state.values() if c.done),
        "path": str(path_for(config)),
    }
