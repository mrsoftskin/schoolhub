"""Reconcile freshly-pulled items against what the calendar already holds.

This is the "did anything new get posted?" core, and it is pure so it can be
tested without a network. Matching mirrors how the audit skeptics worked: an
item is the SAME as an existing event when the course matches and the titles
reduce to the same normalized key; a matched item whose date moved is a MOVE;
an unmatched item is NEW.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import PulledItem

_STOP = {
    "the", "a", "an", "of", "for", "and", "in", "on", "due", "class", "en",
    "de", "la", "el", "los", "las", "before", "no", "real", "date", "target",
}
_KEEP_NUM = re.compile(r"\b(\d+)\b")


def _parts(course: str, title: str) -> tuple[str, list[str], str]:
    """(COURSE, significant content words in order, number signature)."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title.lower())        # drop notes
    t = t.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", t)
    # Joined, not concatenated: "Chapter 1 and 2" and "Chapter 12" both
    # flattened to "12", so a brand-new Chapter 12 quiz read as the Chapter
    # 1-and-2 quiz having MOVED - and apply would then rewrite the older
    # row's date. A separator keeps the runs distinguishable.
    nums = "-".join(_KEEP_NUM.findall(t))
    content, seen = [], set()
    for w in words:
        if w in _STOP or w.isdigit() or w in seen:
            continue
        seen.add(w)
        content.append(w)
    return course.upper(), content, nums


def _key(course: str, title: str) -> str:
    """Normalized (course, title) key.

    Drops bracketed notes and stopwords, keeps any numbers (Chapter 2 !=
    Chapter 3), and signs on only the FIRST THREE significant content words.
    That last part matters: OAKS and the stored calendar often word the same
    assignment differently in trailing detail ("Prueba de verbos 1" vs
    "Prueba de verbos 1: Preterito de verbos regulares"), and a stricter key
    would flag the reworded copy as a brand-new deadline.
    """
    up, content, nums = _parts(course, title)
    return f"{up}|{' '.join(content[:3])}|{nums}"


def _course_tokens(course: str) -> tuple[set[str], str]:
    """The department word and course number that merely restate the course
    itself ("finc", "313" for FINC313). Two FINC313 titles sharing only
    those have nothing in common - every row in the course does."""
    m = re.match(r"^\s*([A-Za-z]+)\s*-?\s*0*(\d+)", course or "")
    if not m:
        return set(), ""
    return {m.group(1).lower()}, m.group(2)


def _cross_worded(course: str, title_a: str, title_b: str) -> bool:
    """Same deadline under different wording: same number signature plus a
    shared content word, where neither is just the course's own name.

    The exclusion is load-bearing. "FINC 313 Lecture" (a one-off at 11:00)
    and "FINC 313 class (Prof. A)" (the recurring 09:00 meeting) share the
    word "finc" and the number "313" and nothing else - matching them
    reported the class as a retimed lecture.
    """
    dept, cnum = _course_tokens(course)
    _, words_a, nums_a = _parts(course, title_a)
    _, words_b, nums_b = _parts(course, title_b)
    if not nums_a or nums_a != nums_b or nums_a == cnum:
        return False
    return bool((set(words_a) & set(words_b)) - dept)


def same_deadline(course_a: str, title_a: str,
                  course_b: str, title_b: str) -> bool:
    """Do these two (course, title) pairs name the same deadline?

    Either the normalized keys agree, or - for the cross-wording case the
    reconciler already understands ("Chapter 2 Assignment" on OAKS vs
    "Connect: Chapter 2" in the csv) - they match on distinctive numbers and
    words.
    """
    if _key(course_a, title_a) == _key(course_b, title_b):
        return True
    if course_a.upper() != course_b.upper():
        return False
    return _cross_worded(course_a, title_a, title_b)


@dataclass
class ExistingEvent:
    course: str
    title: str
    date: str          # YYYY-MM-DD
    source: str = ""
    start_time: str = ""            # HH:MM, or "" for all-day/unknown


@dataclass
class Change:
    item: PulledItem
    kind: str                       # "new" | "moved"
    old_date: str | None = None     # for "moved"
    # Set on a RETIME: same date, but the stored time is no longer any time
    # the source associates with the item (a quiz due moved noon -> 10:00
    # was live-observed on OAKS with sync blind to it - dates matched).
    old_time: str | None = None
    # Every stored time for that slot, so apply can match whichever row
    # actually holds one (two rows can share course+title+date at different
    # times - one from the csv, one from an ics feed).
    old_times: tuple = ()


@dataclass
class Reconciliation:
    new: list[Change] = field(default_factory=list)
    moved: list[Change] = field(default_factory=list)
    present: int = 0                # matched, unchanged

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.moved)

    def summary(self) -> str:
        return f"{len(self.new)} new, {len(self.moved)} moved, {self.present} unchanged"


def reconcile(pulled: list[PulledItem], existing: list[ExistingEvent],
              today: str | None = None) -> Reconciliation:
    # Index existing by normalized key -> set of dates (a title can recur).
    by_key: dict[str, set[str]] = {}
    # (key, date) -> stored HH:MM times, for retime detection.
    by_key_times: dict[tuple[str, str], set[str]] = {}
    # Secondary index for cross-wording matches: (COURSE, number signature) ->
    # [(date, content words)]. Platforms and the stored calendar often name
    # the same deadline with disjoint leading words ("Chapter 2 Assignment"
    # on OAKS vs "Connect: Chapter 2" in the csv), which defeats the primary
    # key. Same course + same numbers + same DATE + at least one shared
    # content word is that same deadline, not a new one.
    by_nums: dict[tuple[str, str], list[tuple[str, frozenset]]] = {}
    for e in existing:
        k = _key(e.course, e.title)
        by_key.setdefault(k, set()).add(e.date)
        # Only rows sync can EDIT are candidates for a retime. A recurring
        # rule or an ics feed is owned elsewhere: reporting it as moved every
        # poll would be noise nothing could ever clear.
        if e.start_time and e.source == "csv":
            by_key_times.setdefault((k, e.date), set()).add(e.start_time)
        up, content, nums = _parts(e.course, e.title)
        if nums:
            by_nums.setdefault((up, nums), []).append(
                (e.date, frozenset(content), e.start_time, e.source))

    # Platform-bucket index: (COURSE, date) -> set of platform words present
    # in existing titles. A VHL day-summary ("Supersite: 4 activities, est
    # 28m") and the calendar's generic "VHL Supersite homework (due before
    # class)" describe the same daily bucket, but share no key and no number
    # signature - the platform word on the same course+date is the identity.
    _PLATFORM_WORDS = {"vhl": {"vhl", "supersite"}}
    by_platform: dict[tuple[str, str], set[str]] = {}
    for e in existing:
        up, content, _ = _parts(e.course, e.title)
        words = set(content)
        for plat_words in _PLATFORM_WORDS.values():
            hit = words & plat_words
            if hit:
                by_platform.setdefault((up, e.date), set()).update(hit)

    def _platform_bucket_present(it: PulledItem) -> bool:
        """Same course + same date + platform bucket already on the calendar.
        Checked BEFORE the primary key: VHL day-summaries repeat counts across
        days ("10 activities" on 9/9 and 12/4), so their keys collide and the
        key path would misread one as the other having moved."""
        plat_words = _PLATFORM_WORDS.get(it.site)
        if not plat_words:
            return False
        up, _, _ = _parts(it.course, it.title)
        return bool(by_platform.get((up, it.date or ""), set()) & plat_words)

    def _same_dated_matches(it: PulledItem) -> list[tuple[str, str]] | None:
        """(stored_time, source) for rows naming this same deadline on this
        same date under different wording ("Chapter 2 Assignment" vs
        "Connect: Chapter 2"). None means no cross-worded match."""
        up, content, nums = _parts(it.course, it.title)
        dept, cnum = _course_tokens(it.course)
        if not nums or nums == cnum:
            return None
        words = set(content)
        hits = [(st, src) for d, ex_words, st, src in by_nums.get((up, nums), [])
                if d == it.date and ((ex_words & words) - dept)]
        return hits or None

    # Every time the SOURCES collectively vouch for at each (key, date), and
    # every (key, date) the sources still serve. Judging a retime per item
    # would flip-flop forever when two feeds describe one deadline with
    # different times (a dropbox close 23:59 vs its calendar event 23:00):
    # each apply would retime the row to the other feed's time.
    times_by_slot: dict[tuple[str, str], set[str]] = {}
    slots_served: set[tuple[str, str]] = set()
    for it in pulled:
        if not it.date:
            continue
        slot = (_key(it.course, it.title), it.date)
        slots_served.add(slot)
        times_by_slot.setdefault(slot, set()).update(
            t for t in (it.start_time, *it.known_times) if t)

    def _retimed(it: PulledItem, k: str, stored: set[str]) -> bool:
        """Is the stored time for this slot one the sources no longer serve?

        Applies on BOTH match paths - the normalized key and the cross-wording
        fallback. Today's live case reached the cross-wording path (OAKS
        appends "- Requires Respondus LockDown Browser" to the stored title),
        so a retime check that lived only on the key path saw nothing while
        the calendar was two hours wrong.
        """
        if not it.start_time or not stored:
            return False
        # Past deadlines are history: a time delta on one (a dropbox close
        # differing from the stored in-class window, seen live on a taken
        # quiz) is churn, not news.
        if today is not None and (it.date or "") < today:
            return False
        return not (stored & times_by_slot.get((k, it.date), set()))

    r = Reconciliation()
    seen_new: set[tuple[str, str]] = set()
    seen_moved: set[tuple[str, str]] = set()
    for it in pulled:
        if not it.date:
            continue  # undated pulled items are not calendar changes
        if _platform_bucket_present(it):
            r.present += 1
            continue
        k = _key(it.course, it.title)
        dates = by_key.get(k)
        if dates is None:
            cross = _same_dated_matches(it)
            if cross is not None:
                stored = {t for t, src in cross if t and src == "csv"}
                if (_retimed(it, k, stored)
                        and (k, it.date) not in seen_moved):
                    seen_moved.add((k, it.date))
                    r.moved.append(Change(item=it, kind="moved",
                                          old_date=it.date,
                                          old_time=sorted(stored)[0],
                                          old_times=tuple(sorted(stored))))
                else:
                    r.present += 1
                continue
            # genuinely new title for this course
            if (k, it.date) in seen_new:
                continue
            seen_new.add((k, it.date))
            r.new.append(Change(item=it, kind="new"))
        elif it.date in dates:
            # Same item, same date - but did its TIME change at the source?
            # Only for single-occurrence items (a recurring series has many
            # dates and per-date times are its own business), and only when
            # the stored time matches NONE of the times the source currently
            # associates with the item (known_times covers a quiz's open/
            # due/close window, so a row stored at the window-open time is
            # not churned to the due time).
            stored = by_key_times.get((k, it.date), set())
            if (len(dates) == 1 and _retimed(it, k, stored)
                    and (k, it.date) not in seen_moved):
                seen_moved.add((k, it.date))
                r.moved.append(Change(item=it, kind="moved", old_date=it.date,
                                      old_time=sorted(stored)[0],
                                      old_times=tuple(sorted(stored))))
            else:
                r.present += 1
        else:
            # same item, different date. Treat as moved only when the existing
            # occurrences are a single date (a recurring series with many
            # dates is not "moved" just because this date is new to it) AND
            # the sources no longer serve that stored date. If some other
            # pulled item still lands there, the stored row is alive and this
            # is an ADDITIONAL occurrence - calling it a move would rewrite a
            # correct deadline's date away.
            only = next(iter(dates))
            if (len(dates) == 1 and (k, only) not in slots_served
                    and (k, only) not in seen_moved):
                seen_moved.add((k, only))
                r.moved.append(Change(
                    item=it, kind="moved", old_date=only,
                    old_times=tuple(sorted(by_key_times.get((k, only), set())))))
            else:
                if (k, it.date) in seen_new:
                    continue
                seen_new.add((k, it.date))
                r.new.append(Change(item=it, kind="new"))
    return r
