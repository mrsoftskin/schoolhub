"""First-run setup helpers for `brain init`.

Turns a blank machine into a working Command Center for a NEW student (not
Carson) without hand-editing config.toml: discover their courses from OAKS,
pick an AI backend + key, geocode a city, and render a portable config. The
pure functions here are unit-tested; the interactive prompts live in cli.init.

Everything the generated config points at is RELATIVE to the config file
(materials/<CODE>, data/), so a friend's config has no machine-specific
absolute paths.
"""

from __future__ import annotations

import re

# Where a generated config looks for a newer build. EMPTY by default, so an
# app nobody packaged for distribution never contacts a server. The packager
# sets it; without it, self-update is silently off on every copy - which is
# exactly what it was, because the wizard never wrote the key at all.
DEFAULT_UPDATE_URL = "https://github.com/mrsoftskin/schoolhub/releases/latest/download/update-manifest.json"

# backend -> (models, default_model, key env var or None, human label, where to get a key)
_BACKEND_INFO = {
    "gemini": (["gemini-2.5-flash"], "gemini-2.5-flash", "GEMINI_API_KEY",
               "Google Gemini (free tier)", "aistudio.google.com/apikey"),
    "openai": (["gpt-4o-mini"], "gpt-4o-mini", "OPENAI_API_KEY",
               "OpenAI", "platform.openai.com/api-keys"),
    "api": (["claude-sonnet-4-6"], "claude-sonnet-4-6", "ANTHROPIC_API_KEY",
            "Anthropic API", "console.anthropic.com/settings/keys"),
    "subscription": (["claude-sonnet-4-6"], "claude-sonnet-4-6", None,
                     "Claude Code login (no key)", ""),
}

BACKEND_CHOICES = ("gemini", "openai", "api", "subscription")


def backend_info(backend: str):
    if backend not in _BACKEND_INFO:
        raise KeyError(f"Unknown backend {backend!r}. Choose from {BACKEND_CHOICES}.")
    return _BACKEND_INFO[backend]


def normalize_code(raw: str) -> str:
    """'finc 313' / 'FINC-313' / 'finc313' -> 'FINC313'."""
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


# The deadline CSV OAKS sync appends to; the header must match calendar.py.
FIXED_CSV_HEADER = "course,title,date,start_time,end_time,all_day,kind\n"


def term_bounds(today=None):
    """Generous (semester_start, semester_end) for the current term, so synced
    deadlines and a friend's Google Calendar events all fall inside the window.
    Exact dates only matter for recurring class meetings, which init does not
    set, so wide bounds are safe."""
    from datetime import date

    d = today or date.today()
    y = d.year
    if d.month >= 8:                      # Fall
        return date(y, 8, 1), date(y, 12, 31)
    if d.month <= 5:                     # Spring
        return date(y, 1, 1), date(y, 5, 31)
    return date(y, 5, 1), date(y, 8, 31)  # Summer


def discover_courses(enrollment_payload: dict, today=None) -> list[dict]:
    """From an OAKS `myenrollments` payload, the current-term course offerings
    as [{code, name, ouid}], deduped and sorted. Uses the SAME term-window and
    course-code parsing as the sync connector, so what init scaffolds is what
    sync will later map."""
    from .connectors.sites import _COURSE_CODE, _current_term

    term = _current_term(today)
    out: list[dict] = []
    seen: set[str] = set()
    for it in enrollment_payload.get("Items", []):
        ou = it.get("OrgUnit") or {}
        if (ou.get("Type") or {}).get("Code") != "Course Offering":
            continue
        name = (ou.get("Name") or "").strip()
        if term not in name:
            continue
        m = _COURSE_CODE.search(name)
        if not m:
            continue
        code = (m.group(1) + m.group(2)).upper()
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "ouid": int(ou["Id"])})
    return sorted(out, key=lambda c: c["code"])


def fetch_enrollments(session: dict) -> dict:
    """Live OAKS myenrollments payload for a stored session."""
    from .connectors import http
    from .connectors.sites import OaksConnector

    oaks = OaksConnector()
    return http.get_json(
        session,
        f"{oaks.base}/d2l/api/lp/{oaks.LP}/enrollments/myenrollments/?isActive=true",
        "oaks",
    )


def geocode(city: str) -> dict | None:
    """City name -> {latitude, longitude, label} via Open-Meteo's free, keyless
    geocoding API. Returns None if nothing matches or the call fails - the
    caller falls back to asking for coordinates (or skipping weather)."""
    import httpx

    try:
        r = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=15.0,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception:
        return None
    if not results:
        return None
    top = results[0]
    parts = [top.get("name"), top.get("admin1"), top.get("country_code")]
    label = ", ".join(p for p in parts if p)
    return {"latitude": top.get("latitude"), "longitude": top.get("longitude"),
            "label": label}


def _course_root(materials_root: str, code: str) -> str:
    """Where one course's files live, as written into config.toml."""
    if materials_root:
        # Absolute, POSIX-style separators (valid in TOML on every OS).
        return materials_root.replace("\\", "/").rstrip("/") + "/" + code
    return f"materials/{code}"


def _toml_str(value: str) -> str:
    """Quote a string for TOML (escape backslashes and double quotes)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(*, update_url: str = DEFAULT_UPDATE_URL, name: str, backend: str, courses: list[str],
                  latitude=None, longitude=None, location_label: str = "",
                  gcal_ics_url: str = "", materials_root: str = "",
                  today=None) -> str:
    """Build a complete, valid config.toml. `courses` is a list of normalized
    codes; each gets a [[collection]] pointing at materials/<CODE>, relative
    to the config by default. materials_root overrides that with an absolute
    location - macOS puts the app's internals in ~/Library/Application
    Support (hidden from Finder) while course files need to live somewhere
    the student can actually find and drop files into. Always writes a [calendar] section (semester bounds + the deadline
    CSV) so OAKS-synced deadlines have somewhere to land; a Google Calendar
    private iCal URL, if given, is subscribed as a feed. Paths stay relative so
    the config is portable between machines."""
    models, default_model, _, _, _ = backend_info(backend)
    start, end = term_bounds(today)
    lines: list[str] = [
        "# Command Center config - generated by `brain init`.",
        "# Drop each course's files into its materials/<CODE> folder, then run",
        "# `brain index`. Deadlines and new files sync from OAKS automatically.",
        "",
        "[settings]",
        # Where this copy looks for a newer build. Empty means
        # self-update is off; the packager fills it in.
        f'update_url = "{update_url}"',
        'data_dir = "data"',
        f"backend = {_toml_str(backend)}",
        f"default_model = {_toml_str(default_model)}",
        "models = [" + ", ".join(_toml_str(m) for m in models) + "]",
        # bge-small floor calibrated on this embedding model (see project memory);
        # same model on a new index, so the same floor applies.
        "similarity_floor = 0.60",
        "soft_similarity_floor = 0.50",
        "",
        "[user]",
        f"name = {_toml_str(name)}",
    ]
    if latitude is not None and longitude is not None:
        lines += [
            f"latitude = {float(latitude)}",
            f"longitude = {float(longitude)}",
            f"location_label = {_toml_str(location_label)}",
        ]
    lines += [
        "",
        "[calendar]",
        # Bare TOML dates (no quotes) - the loader wants real date literals.
        f"semester_start = {start.isoformat()}",
        f"semester_end = {end.isoformat()}",
        # OAKS-synced deadlines are appended here.
        'fixed_csv = "calendar/fixed.csv"',
        # Subscribed feeds: a friend's Google Calendar private iCal link goes
        # here so personal events show alongside course deadlines.
        "ics_urls = [" + (_toml_str(gcal_ics_url) if gcal_ics_url else "") + "]",
        "",
    ]
    for code in courses:
        lines += [
            "[[collection]]",
            f"name = {_toml_str(code)}",
            f'roots = [{_toml_str(_course_root(materials_root, code))}]',
            'assist_level = "full"',
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def verify_backend(backend: str, key: str = "") -> tuple[bool, str]:
    """Make one real call, so a bad key fails HERE and not days later.

    The wizard used to save whatever was pasted and print "All set.", which
    is how a friend ends up with a config that looks correct and a chat that
    never works. The common failure is not a typo: AI Studio happily ISSUES a
    key to a school Google account and then refuses every request with it, so
    the key looks fine by inspection and only a live call reveals it.

    Returns (ok, why-not). Never raises: a wizard that dies on a flaky network
    is worse than one that warns and moves on.
    """
    import os

    _, default_model, env_var, _, _ = backend_info(backend)
    if backend == "subscription":
        # No key to test; what can be wrong is the Claude Code CLI missing.
        try:
            from .agentsdk import available

            return available()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    if not env_var:
        return True, ""
    if not key:
        return False, "no key given"
    previous = os.environ.get(env_var)
    os.environ[env_var] = key
    try:
        from . import providers

        stream = providers.stream(
            backend, "Reply with the single word: ok",
            "You are a connectivity test. Reply with one word.",
            default_model, [], None, 16,
        )
        got = "".join(stream).strip()
        if got:
            return True, ""
        return False, "the service accepted the key but sent nothing back"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        # Leave the process as we found it; the key's real home is .env, which
        # load_env_file reads on every subsequent command.
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous


def ensure_update_url(config_path, url: str = "") -> str:
    """Add `update_url` to an existing config that predates the setting.

    Without this, every copy installed before self-update existed stays on
    manual updates FOREVER: re-running the installer and declining the
    overwrite (the correct answer, since it preserves their courses, index and
    deadlines) leaves the old config in place, and updates._manifest_url reads
    settings.update_url with NO fallback to DEFAULT_UPDATE_URL. So the person
    who most needs the updater is the one guaranteed not to get it.

    Additive and idempotent: only ever inserts a missing key under [settings],
    never edits an existing value or touches anything else in the file.
    Returns the URL written, or "" if nothing changed.
    """
    import re
    from pathlib import Path

    url = (url or DEFAULT_UPDATE_URL).strip()
    if not url:
        return ""
    path = Path(config_path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    # Already set (even to something else): the user's choice wins.
    if re.search(r"^\s*update_url\s*=", text, re.M):
        return ""
    m = re.search(r"^\[settings\]\s*$", text, re.M)
    if not m:
        return ""
    block = "\n# Where this copy looks for a newer build.\n" + f'update_url = "{url}"'
    at = m.end()
    path.write_text(text[:at] + block + text[at:], encoding="utf-8", newline="")
    return url


def write_env_key(env_path, var: str, value: str) -> None:
    """Set var=value in a .env file, replacing an existing line for that var
    and creating the file if needed. Values are written raw (no quoting) to
    match env.py's simple parser."""
    from pathlib import Path

    env_path = Path(env_path)
    existing = []
    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in existing:
        if line.strip() and not line.lstrip().startswith("#") \
                and line.split("=", 1)[0].strip() == var:
            out.append(f"{var}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{var}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
