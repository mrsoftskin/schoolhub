"""Self-diagnosis: one command that says what is wrong and how to fix it.

Written for the person who did NOT build this app. A friend running Command
Center on their own Mac has no Claude Code, no terminal fluency, and no way to
ask the author what "MissingAPIKeyError" means. So every check here answers
three questions in plain words: what was tested, whether it passed, and the one
thing to do about it if it did not.

The report is meant to be SENT to whoever set them up, which makes redaction a
correctness requirement, not a nicety: this file must never print an API key, a
session cookie, or a private calendar URL. Values are described (present, how
long, what it starts with) instead of shown - see `_redact`.

Checks are ordered the way failures cascade, from "the app cannot start at all"
down to "it runs but has nothing in it", so the first FAIL is almost always the
real one.
"""

from __future__ import annotations

import re

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"

# Anything whose VALUE is a credential. Matched case-insensitively against env
# var names and against key names inside session files.
_SECRET_HINTS = ("key", "token", "secret", "password", "cookie", "session",
                 "auth", "erights", "d2l", "sig", "signature", "policy",
                 "credential")


def _looks_secret(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _SECRET_HINTS)


def _redact(value: str | None, *, show_prefix: int = 0) -> str:
    """Describe a secret without disclosing it.

    show_prefix reveals a few leading characters when the PREFIX itself is
    diagnostic (a Google key starts "AIza"; a pasted Anthropic key starts
    "sk-ant-"), which is how you tell "wrong kind of key" from "no key" without
    ever transmitting the credential.
    """
    if value is None:
        return "missing"
    v = str(value)
    if not v:
        return "empty"
    head = f", starts {v[:show_prefix]}" if show_prefix and len(v) > show_prefix else ""
    return f"present, {len(v)} chars{head}"


def _tilde(p) -> str:
    """Path with the user's home folder collapsed to ~, so the report does not
    carry their account name."""
    try:
        return "~/" + str(Path(p).resolve().relative_to(Path.home())).replace("\\", "/")
    except (ValueError, OSError):
        return str(p)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list = field(default_factory=list)

    def add(self, name, status, detail="", fix="") -> None:
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failures(self) -> list:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list:
        return [c for c in self.checks if c.status == WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def to_text(self) -> str:
        lines = ["Command Center - diagnostic report", "=" * 52, ""]
        width = max((len(c.name) for c in self.checks), default=10)
        for c in self.checks:
            lines.append(f"[{c.status:4}] {c.name.ljust(width)}  {c.detail}")
            if c.fix and c.status in (FAIL, WARN):
                lines.append(f"        -> {c.fix}")
        lines += ["", "-" * 52]
        if self.failures:
            lines.append(f"{len(self.failures)} problem(s) found. "
                         f"Fix the FIRST one listed above - the rest often "
                         f"clear up with it.")
        elif self.warnings:
            lines.append("Everything essential works. The WARN lines are "
                         "optional extras.")
        else:
            lines.append("Everything checks out.")
        lines.append("This report hides your keys, passwords and cookies, so "
                     "it is safe to send.")
        return "\n".join(lines)


# ---- individual checks ---------------------------------------------------

def _check_platform(r: Report) -> None:
    bits = f"{platform.system()} {platform.release()}, {platform.machine()}"
    if sys.platform == "darwin":
        bits = f"macOS {platform.mac_ver()[0]}, {platform.machine()}"
    r.add("computer", INFO, bits)
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro} at {_tilde(sys.executable)}"
    if v < (3, 12):
        r.add("python", FAIL, detail,
              "This app needs Python 3.12. Re-run the installer.")
    else:
        r.add("python", OK, detail)


def _check_disk(r: Report, data_dir: Path | None) -> None:
    target = data_dir if data_dir and data_dir.exists() else Path.home()
    try:
        free_gb = shutil.disk_usage(target).free / 1e9
    except OSError as e:
        r.add("disk space", WARN, f"could not read ({e})")
        return
    if free_gb < 1:
        r.add("disk space", FAIL, f"{free_gb:.1f} GB free",
              "Free up space - indexing and the AI model need room to write.")
    elif free_gb < 5:
        r.add("disk space", WARN, f"{free_gb:.1f} GB free",
              "Getting tight; the search model alone needs ~130 MB.")
    else:
        r.add("disk space", OK, f"{free_gb:.1f} GB free")


def _check_package(r: Report) -> None:
    try:
        import brain

        where = _tilde(Path(brain.__file__).parent)
        # The version is the first thing to establish when someone reports a
        # bug: half of "it's broken" is really "you're three builds behind".
        version = getattr(brain, "__version__", "unknown")
        r.add("app version", INFO, version)
        r.add("app installed", OK, where)
    except Exception as e:            # pragma: no cover - import always works here
        r.add("app installed", FAIL, f"{type(e).__name__}: {e}",
              "The app did not install correctly. Re-run the installer.")


def _load_config(r: Report, config_path):
    """Find and parse config.toml; returns a Config or None."""
    from .config import find_config, load_config

    try:
        path = find_config(config_path)
    except Exception as e:
        r.add("settings file", FAIL, str(e)[:160],
              "config.toml was not found. Re-run the installer, or start the "
              "app once so setup can create it.")
        return None
    try:
        cfg = load_config(path)
    except Exception as e:
        r.add("settings file", FAIL, f"{_tilde(path)}: {str(e)[:140]}",
              "config.toml is present but unreadable. Send this report to "
              "whoever set you up - the file needs one line corrected.")
        return None
    r.add("settings file", OK, _tilde(path))
    return cfg


def _check_data_dir(r: Report, cfg) -> None:
    d = Path(cfg.settings.data_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.add("data folder", OK, _tilde(d))
    except OSError as e:
        r.add("data folder", FAIL, f"{_tilde(d)}: {e}",
              "The app cannot write to its own folder. Check the folder still "
              "exists and is not on a disconnected drive.")


def _check_database(r: Report, cfg) -> None:
    db = Path(cfg.settings.data_dir) / "brain.db"
    if not db.exists():
        r.add("database", WARN, "not created yet",
              "Normal before the first index. Open the app once.")
        return
    try:
        import sqlite3

        con = sqlite3.connect(db)
        files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.close()
    except Exception as e:
        r.add("database", FAIL, f"{type(e).__name__}: {str(e)[:100]}",
              "The database is damaged. Delete data/brain.db and re-open the "
              "app to rebuild it (your course files are untouched).")
        return
    detail = f"{files} files, {chunks} searchable pieces, {events} calendar items"
    if chunks == 0:
        r.add("database", WARN, detail,
              "Nothing is indexed yet. Put course files in your Command "
              "Center folder and re-open the app.")
    else:
        r.add("database", OK, detail)


def _check_courses(r: Report, cfg) -> None:
    if not cfg.collections:
        r.add("courses", FAIL, "none configured",
              "No courses are set up. Re-run setup to add them.")
        return
    missing = []
    for col in cfg.collections:
        if not any(Path(root).exists() for root in col.roots):
            missing.append(col.name)
    names = ", ".join(c.name for c in cfg.collections)
    if missing:
        r.add("courses", WARN, f"{len(cfg.collections)} set up ({names}); "
                               f"folder missing for {', '.join(missing)}",
              "Those course folders are gone or renamed. Create them inside "
              "your Command Center folder.")
    else:
        r.add("courses", OK, f"{len(cfg.collections)} set up ({names})")


def _check_model(r: Report, cfg, *, offline: bool) -> None:
    """Is the search model on disk? Downloading it is the slowest first-run
    step and the one most likely to have failed silently."""
    name = cfg.settings.embedding_model
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        r.add("search model", WARN, "cannot check (huggingface_hub missing)")
        return
    hit = None
    for candidate in ("onnx/model.onnx", "tokenizer.json"):
        try:
            got = try_to_load_from_cache(name, candidate)
        except Exception:
            got = None
        if isinstance(got, str):
            hit = got
            break
    if hit:
        r.add("search model", OK, f"{name} (downloaded)")
        return
    if offline:
        r.add("search model", WARN, f"{name} not downloaded yet",
              "It downloads (~130 MB) the first time you ask a question.")
        return
    r.add("search model", WARN, f"{name} not downloaded yet",
          "Connect to the internet and ask one question - it downloads "
          "~130 MB once, then works offline.")


def _check_backend(r: Report, cfg, *, offline: bool) -> None:
    """Is an AI backend configured, and does its key actually work?

    Key PRESENCE and key VALIDITY are different failures with the same
    symptom ("chat does nothing"), so when we are allowed on the network we
    make one real, minimal call.
    """
    from . import providers

    backend = cfg.settings.backend
    if backend == "subscription":
        try:
            from . import agentsdk

            ok, why = agentsdk.available()
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            r.add("AI backend", OK, "Claude Code subscription")
        else:
            r.add("AI backend", FAIL, f"subscription: {why[:110]}",
                  "This copy is set to use Claude Code, which is not "
                  "available here. Re-run setup and choose Gemini instead.")
        return

    var = providers.env_var_for(backend)
    key = os.environ.get(var) if var else None
    prefix = 4 if backend == "gemini" else 7
    shown = _redact(key, show_prefix=prefix)
    if not key:
        r.add("AI backend", FAIL, f"{backend}: {var} {shown}",
              f"No AI key found. Get a free one at "
              f"aistudio.google.com/apikey (sign in with a PERSONAL Gmail, "
              f"not your school account) and put it in your .env file as "
              f"{var}=your-key")
        return
    if offline:
        r.add("AI backend", OK, f"{backend}: {var} {shown} (not tested)")
        return
    ok, detail = _live_backend_test(cfg, backend)
    if ok:
        r.add("AI backend", OK, f"{backend}: {var} {shown}, answered a test question")
    else:
        r.add("AI backend", FAIL, f"{backend}: {var} {shown}, but {detail[:110]}",
              "The key is there but the AI service rejected it. If you made "
              "it with a SCHOOL Google account, that is usually the cause - "
              "make a new key with a personal Gmail.")


def _live_backend_test(cfg, backend: str) -> tuple[bool, str]:
    """One tiny real call. Returns (ok, why-not)."""
    from . import providers

    try:
        stream = providers.stream(
            backend, "Reply with the single word: ok",
            "You are a connectivity test. Reply with one word.",
            cfg.settings.default_model, [], None, 16,
        )
        got = "".join(list(stream)).strip()
        return (True, "") if got else (False, "the service returned nothing")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_sessions(r: Report, cfg) -> None:
    """Saved course-site logins. These expire, and an expired one is the most
    common reason deadlines stop updating."""
    from .connectors import REGISTRY, SessionStore

    store = SessionStore(cfg.settings.data_dir)
    have, stale = [], []
    for name in REGISTRY:
        if not store.has(name):
            continue
        try:
            age = store.age_hours(name)
        except Exception:
            age = None
        if age is None:
            have.append(name)
        elif age > 336:                       # two weeks
            stale.append(f"{name} ({age / 24:.0f}d old)")
        else:
            have.append(f"{name} ({age:.0f}h old)")
    if not have and not stale:
        r.add("course logins", WARN, "none saved",
              "Deadlines will not sync. Install the Chrome helper (see the "
              "guide) and log into OAKS in Chrome.")
        return
    detail = ", ".join(have + stale)
    if stale:
        r.add("course logins", WARN, detail,
              "Log into those sites again in Chrome; the helper refreshes "
              "them automatically once you do.")
    else:
        r.add("course logins", OK, detail)


def _check_extension(r: Report, cfg) -> None:
    app_dir = Path(cfg.settings.data_dir).parent
    ext = app_dir / "browser-extension"
    if ext.exists() and (ext / "manifest.json").exists():
        r.add("chrome helper", OK, f"ready to load from {_tilde(ext)}")
    else:
        r.add("chrome helper", WARN, "files not found",
              "Re-run the installer to restore the Chrome helper folder.")


def _check_server(r: Report, cfg) -> None:
    """Is the app already running, or is something else on its port?"""
    import socket

    port = int(os.environ.get("CC_PORT") or 8177)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        listening = s.connect_ex(("127.0.0.1", port)) == 0
    if not listening:
        r.add("app running", INFO, f"not running (port {port} free)")
        return
    try:
        import urllib.request

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=4) as resp:
            ours = resp.status == 200
    except Exception:
        ours = False
    if ours:
        r.add("app running", OK, f"yes, on port {port}")
    else:
        r.add("app running", FAIL, f"another program is using port {port}",
              f"Quit whatever else uses port {port}, or start the app with a "
              f"different one: CC_PORT=8178")


# Only ever emit a name that matches this shape, or one of the app's own
# exception names. An allowlist rather than a heuristic, because anything
# that escapes here lands in a report the student is told is safe to send.
_EXC_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{0,38}(?:Error|Exception|Warning))\b")


def _app_exception_names() -> frozenset[str]:
    from . import errors as _errors

    return frozenset(
        n for n in dir(_errors)
        if n[:1].isupper() and isinstance(getattr(_errors, n), type)
    )


def _exception_kind(line: str) -> str:
    """Name the failure without quoting it.

    Returns something like "HTTPStatusError" or "LoginRequired". Everything
    else on that line - URLs, cookie values, key fragments, file contents - is
    discarded, because this feeds a report the student is told is safe to
    send. Nothing reaches the output that did not match the strict pattern
    above or come from the app's own exception list.
    """
    text = line or ""
    m = _EXC_RE.search(text)
    if m:
        return m.group(1)
    for name in sorted(_app_exception_names() | {"LoginRequired"}):
        if name in text:
            return name
    if "Traceback" in text:
        return "Traceback"
    return "an error (see the log)"


def _check_logs(r: Report, cfg) -> None:
    log = Path(cfg.settings.data_dir) / "logs" / "server.log"
    if not log.exists():
        r.add("log file", INFO, "none yet")
        return
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        r.add("log file", WARN, f"unreadable ({e})")
        return
    bad = [ln for ln in lines[-400:]
           if ("Traceback" in ln or "ERROR" in ln or "Exception" in ln)]
    where = _tilde(log)
    if bad:
        # The EXCEPTION TYPE, never the message. This report is written to
        # diagnostic.txt and the guide tells the student in writing that it
        # "never includes the actual keys, passwords, or cookies" - and
        # launch.py points both stdout and stderr at this log, so an
        # arbitrary traceback line can carry a session cookie, an API key, or
        # a secret calendar URL. A raw slice broke that promise on exactly the
        # runs where the report gets sent.
        r.add("log file", WARN, f"{where} - {len(bad)} recent error line(s); "
                                f"last: {_exception_kind(bad[-1])}",
              "Send this report if the app is misbehaving. It names the kind "
              "of error only; open the log yourself for the full text.")
    else:
        r.add("log file", OK, f"{where} - no recent errors")


def _check_calendar(r: Report, cfg) -> None:
    if not cfg.calendar:
        r.add("calendar", WARN, "not configured",
              "Re-run setup so deadlines have somewhere to land.")
        return
    csv_path = cfg.calendar.fixed_csv
    if csv_path and Path(csv_path).exists():
        try:
            rows = sum(1 for _ in open(csv_path, encoding="utf-8-sig")) - 1
        except OSError:
            rows = -1
        r.add("calendar", OK, f"{max(rows, 0)} deadline row(s) in "
                              f"{_tilde(csv_path)}")
    else:
        r.add("calendar", WARN, "deadline file missing",
              "Re-run setup; deadlines have nowhere to save.")


# ---- entry point ---------------------------------------------------------

def run(config_path=None, *, offline: bool = False) -> Report:
    """Run every check. `offline` skips the two network-touching ones."""
    r = Report()
    _check_platform(r)
    _check_package(r)

    # .env holds the API key and is loaded relative to the config, so this
    # must happen before the backend check.
    try:
        from .config import find_config
        from .env import load_env_file

        load_env_file(find_config(config_path))
    except Exception:
        pass

    cfg = _load_config(r, config_path)
    if cfg is None:
        _check_disk(r, None)
        return r

    _check_data_dir(r, cfg)
    _check_disk(r, Path(cfg.settings.data_dir))
    _check_backend(r, cfg, offline=offline)
    _check_model(r, cfg, offline=offline)
    _check_courses(r, cfg)
    _check_database(r, cfg)
    _check_calendar(r, cfg)
    _check_sessions(r, cfg)
    _check_extension(r, cfg)
    _check_server(r, cfg)
    _check_logs(r, cfg)
    return r
