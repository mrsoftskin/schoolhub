"""Load and validate config.toml.

Fail-loud policy: unknown assist levels, bad colors, malformed calendar rules,
and duplicate collection names are hard errors at load time. Nonexistent root
paths do NOT block loading (a moved folder should not brick the whole app) but
are collected into Config.warnings, which every caller is expected to surface.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from .errors import ConfigError

ASSIST_LEVELS = ("full", "explain_only", "off")
EVENT_KINDS = ("exam", "project", "quiz", "recurring", "admin")
WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_INCLUDE = [
    "**/*.pdf", "**/*.docx", "**/*.pptx", "**/*.md", "**/*.txt",
    "**/*.xlsx", "**/*.html", "**/*.htm",
]
# Always skipped, on top of any user excludes. macOS writes AppleDouble
# sidecars ("._Lecture.pdf") next to real files and __MACOSX/ folders inside
# zips; both match the include globs and would index as garbage. Windows'
# Thumbs.db and Finder's .DS_Store are the same kind of noise.
_DEFAULT_EXCLUDE = [
    "**/._*", "**/__MACOSX/**", "**/.DS_Store", "**/Thumbs.db",
]


@dataclass
class Collection:
    name: str
    roots: list[Path]
    include: list[str]
    exclude: list[str]
    assist_level: str
    color: str


@dataclass
class RecurringRule:
    course: str
    title: str
    weekdays: list[int]          # 0=Monday .. 6=Sunday
    start: time
    end: time
    # "recurring" = a class meeting (excluded from deadline views). Any
    # deadline kind makes this a repeating piece of graded work - homework due
    # before every class, a weekly problem set - which belongs in Next up and
    # the due-soon count like any other deadline.
    kind: str = "recurring"


@dataclass
class BreakRange:
    start: date
    end: date
    label: str = ""

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass
class CalendarConfig:
    ics_paths: list[Path]
    # Subscribed ICS feeds (OAKS/D2L, Google Calendar). A feed is refetched on
    # every import, so new or moved items appear without re-exporting a file.
    ics_urls: list[str]
    fixed_csv: Path | None
    semester_start: date
    semester_end: date
    recurring: list[RecurringRule]
    breaks: list[BreakRange]


# How the chat answer is generated. All backends run the SAME retrieval,
# similarity floor, and assist gate first (in prepare_ask); they differ only in
# who bills the request. "subscription" uses the local Claude Code login (no
# key). The rest use an API key from .env: "api" = Anthropic (ANTHROPIC_API_KEY),
# "openai" = OpenAI (OPENAI_API_KEY), "gemini" = Google Gemini (GEMINI_API_KEY,
# which has a free tier - the option for someone with no paid LLM plan). When a
# key-based backend is chosen, settings.models must list that vendor's model
# names (e.g. gemini-2.5-flash), not Claude ids.
BACKENDS = ("subscription", "api", "openai", "gemini")


@dataclass
class Settings:
    data_dir: Path
    similarity_floor: float = 0.3
    # Below the hard floor but at/above this, a query is answered anyway with a
    # low-confidence notice instead of a flat refusal. Short questions
    # ("attendance policy?") score low even when the answer is genuinely
    # indexed; a hard refusal there is worse than a hedged answer. Set equal to
    # similarity_floor to disable the soft band.
    soft_similarity_floor: float = 0.5
    context_token_budget: int = 8000
    default_model: str = "claude-sonnet-4-6"
    models: list[str] = field(default_factory=lambda: ["claude-sonnet-4-6", "claude-fable-5"])
    top_k: int = 6
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # "subscription": answer through the Claude Code login (no API key, drawn
    # from a Pro/Max plan). "api": answer through ANTHROPIC_API_KEY, billed
    # pay-as-you-go. Retrieval, citations, and the assist gate are identical.
    backend: str = "subscription"
    max_budget_usd: float | None = None
    # Sites the student does not use, skipped entirely by the sync. Without
    # this, the sync runs every connector in REGISTRY and reports each one it
    # cannot reach as a failure, so someone who only uses OAKS gets permanent
    # warnings naming services they have never heard of and cannot fix. An
    # explicit `--site X` still runs X, so a disabled site stays testable.
    sync_sites_off: list[str] = field(default_factory=list)
    # Background assignment-sync poll interval for `brain serve`, in minutes.
    # 0 disables it. The poll is always a DRY RUN - it reports new/moved
    # deadlines, never writes the calendar (applying stays a manual choice).
    sync_poll_minutes: int = 360
    # URL of a JSON manifest {version, wheel_url, sha256, notes} describing
    # the newest published build. EMPTY BY DEFAULT: an app that contacts a
    # server the owner did not configure is not something to ship by
    # accident, so self-update is opt-in per install.
    update_url: str = ""


@dataclass
class UserConfig:
    """Who this instance belongs to, for the greeting and local weather."""
    name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    location_label: str = ""

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass
class Config:
    path: Path
    collections: list[Collection]
    calendar: CalendarConfig | None
    settings: Settings
    user: UserConfig = field(default_factory=UserConfig)
    warnings: list[str] = field(default_factory=list)

    def collection(self, name: str) -> Collection:
        for c in self.collections:
            if c.name == name:
                return c
        known = ", ".join(c.name for c in self.collections)
        raise ConfigError(f"Unknown collection '{name}'. Known collections: {known}")

    def collection_names(self) -> list[str]:
        return [c.name for c in self.collections]


def _parse_time(value: str, where: str) -> time:
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not m:
        raise ConfigError(f"{where}: time '{value}' is not HH:MM")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError(f"{where}: time '{value}' out of range")
    return time(hh, mm)


def _require_date(value: object, where: str) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    raise ConfigError(f"{where}: expected a TOML date (YYYY-MM-DD), got {value!r}")


def _resolve(base: Path, p: str) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")
    try:
        # tomllib reads bytes and rejects a UTF-8 BOM, which Windows editors
        # and PowerShell's `Set-Content -Encoding utf8` add routinely. Decode
        # ourselves so a BOM is a non-event rather than "invalid TOML line 1".
        raw = tomllib.loads(cfg_path.read_bytes().decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Config file {cfg_path} is not valid TOML: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"Config file {cfg_path} is not valid UTF-8: {e}") from e

    base = cfg_path.parent
    warnings: list[str] = []

    # ---- settings -------------------------------------------------------
    s = raw.get("settings", {})
    settings = Settings(
        data_dir=_resolve(base, s.get("data_dir", "data")),
        similarity_floor=float(s.get("similarity_floor", 0.3)),
        soft_similarity_floor=float(s.get("soft_similarity_floor", 0.5)),
        context_token_budget=int(s.get("context_token_budget", 8000)),
        default_model=s.get("default_model", "claude-sonnet-4-6"),
        models=list(s.get("models", ["claude-sonnet-4-6", "claude-fable-5"])),
        top_k=int(s.get("top_k", 6)),
        embedding_model=s.get("embedding_model", "BAAI/bge-small-en-v1.5"),
        backend=s.get("backend", "subscription"),
        max_budget_usd=(float(s["max_budget_usd"]) if s.get("max_budget_usd") is not None else None),
        sync_sites_off=[str(x).strip().lower()
                        for x in (s.get("sync_sites_off") or [])
                        if str(x).strip()],
        sync_poll_minutes=int(s.get("sync_poll_minutes", 360)),
        update_url=str(s.get("update_url", "") or ""),
    )
    if settings.backend not in BACKENDS:
        raise ConfigError(
            f"settings.backend must be one of {BACKENDS}, got {settings.backend!r}"
        )
    if settings.max_budget_usd is not None and settings.max_budget_usd <= 0:
        raise ConfigError("settings.max_budget_usd must be greater than 0")
    if not (0.0 <= settings.similarity_floor <= 1.0):
        raise ConfigError(f"settings.similarity_floor must be in [0, 1], got {settings.similarity_floor}")
    if settings.context_token_budget < 500:
        raise ConfigError("settings.context_token_budget must be at least 500")
    if settings.top_k < 1:
        raise ConfigError(f"settings.top_k must be at least 1, got {settings.top_k}")
    if settings.sync_sites_off:
        # A typo here would silently disable nothing, and the warning it was
        # meant to silence would keep appearing with no clue why.
        from .connectors import REGISTRY

        unknown = [x for x in settings.sync_sites_off if x not in REGISTRY]
        if unknown:
            raise ConfigError(
                f"settings.sync_sites_off names unknown site(s) {unknown}. "
                f"Known sites: {', '.join(sorted(REGISTRY))}"
            )
    if settings.default_model not in settings.models:
        raise ConfigError(
            f"settings.default_model '{settings.default_model}' is not in settings.models {settings.models}"
        )

    # ---- collections ----------------------------------------------------
    raw_collections = raw.get("collection", [])
    if not raw_collections:
        raise ConfigError(f"No [[collection]] entries in {cfg_path}")
    collections: list[Collection] = []
    seen: set[str] = set()
    for i, rc in enumerate(raw_collections):
        where = f"[[collection]] #{i + 1}"
        name = rc.get("name")
        if not name:
            raise ConfigError(f"{where}: missing 'name'")
        if name.lower() == "all":
            raise ConfigError(f"{where}: 'all' is reserved for global mode and cannot be a collection name")
        if name in seen:
            raise ConfigError(f"{where}: duplicate collection name '{name}'")
        seen.add(name)

        roots_raw = rc.get("roots") or rc.get("root")
        if not roots_raw:
            raise ConfigError(f"{where} ({name}): missing 'roots'")
        if isinstance(roots_raw, str):
            roots_raw = [roots_raw]
        roots = [_resolve(base, r) for r in roots_raw]
        for r in roots:
            if not r.exists():
                warnings.append(f"Collection '{name}': root path does not exist: {r}")

        level = rc.get("assist_level")
        if level not in ASSIST_LEVELS:
            raise ConfigError(
                f"{where} ({name}): assist_level must be one of {ASSIST_LEVELS}, got {level!r}"
            )
        color = rc.get("color", "#888888")
        if not _COLOR_RE.match(color):
            raise ConfigError(f"{where} ({name}): color '{color}' is not #rrggbb hex")

        collections.append(Collection(
            name=name,
            roots=roots,
            include=list(rc.get("include", _DEFAULT_INCLUDE)),
            exclude=_DEFAULT_EXCLUDE + list(rc.get("exclude", [])),
            assist_level=level,
            color=color,
        ))

    # ---- calendar -------------------------------------------------------
    calendar: CalendarConfig | None = None
    rcal = raw.get("calendar")
    if rcal is not None:
        semester_start = _require_date(rcal.get("semester_start"), "[calendar].semester_start")
        semester_end = _require_date(rcal.get("semester_end"), "[calendar].semester_end")
        if semester_end < semester_start:
            raise ConfigError("[calendar]: semester_end is before semester_start")

        # A URL in ics_paths is a subscribed feed, not a file on disk; accept
        # it there too so an existing config does not need restructuring.
        raw_ics = list(rcal.get("ics_paths", [])) + list(rcal.get("ics_urls", []))
        ics_urls = [s for s in raw_ics if str(s).startswith(("http://", "https://"))]
        ics_paths = [_resolve(base, p) for p in raw_ics if p not in ics_urls]
        for p in ics_paths:
            if not p.exists():
                warnings.append(f"Calendar: ics path does not exist: {p}")

        fixed_csv = None
        if rcal.get("fixed_csv"):
            fixed_csv = _resolve(base, rcal["fixed_csv"])
            if not fixed_csv.exists():
                warnings.append(f"Calendar: fixed_csv does not exist: {fixed_csv}")

        recurring: list[RecurringRule] = []
        for i, rr in enumerate(rcal.get("recurring", [])):
            where = f"[[calendar.recurring]] #{i + 1}"
            for key in ("course", "title", "weekdays", "start", "end"):
                if key not in rr:
                    raise ConfigError(f"{where}: missing '{key}'")
            weekdays = []
            for w in rr["weekdays"]:
                wl = str(w).strip().lower()
                if wl not in WEEKDAY_NAMES:
                    raise ConfigError(f"{where}: unknown weekday '{w}'")
                weekdays.append(WEEKDAY_NAMES[wl])
            start_t = _parse_time(rr["start"], where)
            end_t = _parse_time(rr["end"], where)
            if end_t <= start_t:
                raise ConfigError(f"{where}: end time {rr['end']} is not after start {rr['start']}")
            kind = rr.get("kind", "recurring")
            if kind not in EVENT_KINDS:
                raise ConfigError(f"{where}: kind must be one of {EVENT_KINDS}, got {kind!r}")
            recurring.append(RecurringRule(
                course=rr["course"], title=rr["title"],
                weekdays=sorted(set(weekdays)), start=start_t, end=end_t,
                kind=kind,
            ))

        breaks: list[BreakRange] = []
        for i, rb in enumerate(rcal.get("breaks", [])):
            where = f"[[calendar.breaks]] #{i + 1}"
            bstart = _require_date(rb.get("start"), f"{where}.start")
            bend = _require_date(rb.get("end", rb.get("start")), f"{where}.end")
            if bend < bstart:
                raise ConfigError(f"{where}: end before start")
            breaks.append(BreakRange(start=bstart, end=bend, label=rb.get("label", "")))

        calendar = CalendarConfig(
            ics_paths=ics_paths,
            ics_urls=ics_urls,
            fixed_csv=fixed_csv,
            semester_start=semester_start,
            semester_end=semester_end,
            recurring=recurring,
            breaks=breaks,
        )

    # ---- user -----------------------------------------------------------
    ru = raw.get("user", {})
    lat, lon = ru.get("latitude"), ru.get("longitude")
    if (lat is None) != (lon is None):
        raise ConfigError("[user]: latitude and longitude must be set together")
    if lat is not None and not (-90 <= float(lat) <= 90):
        raise ConfigError(f"[user].latitude {lat} is out of range")
    if lon is not None and not (-180 <= float(lon) <= 180):
        raise ConfigError(f"[user].longitude {lon} is out of range")
    user = UserConfig(
        name=ru.get("name", ""),
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        location_label=ru.get("location_label", ""),
    )

    return Config(
        path=cfg_path,
        collections=collections,
        calendar=calendar,
        settings=settings,
        user=user,
        warnings=warnings,
    )


def installed_config_paths() -> list[Path]:
    """Where an INSTALLED copy keeps its config, per platform.

    A developer runs commands from the repo, so walking up from cwd finds the
    config. Someone who ran the installer has no repo and will type commands
    in a home-directory Terminal, where walking up finds nothing - these are
    the paths that make `brain doctor` work for them at all.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "CommandCenter" / "config.toml"]
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return [Path(base) / "SchoolHub" / "config.toml"] if base else []
    return [home / ".config" / "CommandCenter" / "config.toml"]


def find_config(explicit: str | Path | None = None) -> Path:
    """Locate config.toml: explicit arg, BRAIN_CONFIG, cwd and its parents,
    then the installed-app location for this platform."""
    if explicit:
        return Path(explicit)
    # The launcher already exports this for the running app; honoring it here
    # means the CLI and the app can never disagree about which config is live.
    env = os.environ.get("BRAIN_CONFIG")
    if env:
        return Path(env)
    cur = Path.cwd().resolve()
    for candidate in [cur, *cur.parents]:
        p = candidate / "config.toml"
        if p.exists():
            return p
    for p in installed_config_paths():
        if p.exists():
            return p
    raise ConfigError(
        "No config.toml found in the current directory or any parent, and no "
        "installed copy in the usual place. Pass --config, or run from the "
        "project root."
    )
