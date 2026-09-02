"""Self-update: tell the user a new build exists, and install it safely.

Shape of the thing, and why:

A friend's copy is an installed WHEEL inside a venv they never see. So an
update is not "pull the source" - it is "replace one package in that venv".
The catch is that the app is SERVING from that venv when the user clicks
Update, and swapping files under a running interpreter leaves it half on the
old code and half on the new. So this never installs into the live process:

    click Update  ->  download + verify + STAGE on disk
    next launch   ->  launch.py applies the staged wheel BEFORE serving

which is the one moment nothing is imported yet. The user quits and reopens,
which they already do daily.

Two safety rules that are not optional:
  - the download is verified against a SHA-256 from the manifest before it is
    ever installed, so a truncated download or a swapped file cannot land;
  - the update URL is EMPTY by default. Nothing contacts the network unless
    the person who packaged the app configured a manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PENDING = "pending.json"
TIMEOUT = 15

# The staged file MUST keep a valid wheel name ("schoolhub-0.2.0-py3-none-any
# .whl"). uv reads the version out of the FILENAME and refuses anything else -
# "The wheel filename 'pending.whl' is invalid: Must have a version" - and uv
# is the only installer present on a Mac, so a generic name breaks every Mac
# update while working fine on Windows, where pip is more forgiving.
_WHEEL_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def wheel_name(version: str, url: str = "") -> str:
    """A safe, valid wheel filename for a staged download."""
    base = (url or "").rsplit("/", 1)[-1].split("?")[0]
    if (base.lower().endswith(".whl") and base.count("-") >= 2
            and set(base) <= _WHEEL_CHARS and ".." not in base):
        return base                      # publisher's own name, sanitized
    safe = "".join(c for c in str(version) if c in "0123456789.") or "0"
    return f"schoolhub-{safe}-py3-none-any.whl"


def _updates_dir(config) -> Path:
    return Path(config.settings.data_dir) / "updates"


def parse_version(v: str) -> tuple:
    """Dotted numeric compare, tolerant of suffixes ('0.2.0rc1' -> (0,2,0)).

    Deliberately tiny: `packaging` is not a declared dependency, and an
    updater that crashes on a version string is worse than one that is
    slightly naive about pre-releases.
    """
    out = []
    for part in str(v or "").split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    return tuple(out or [0])


def current_version() -> str:
    import brain

    return getattr(brain, "__version__", "0")


@dataclass
class Available:
    version: str
    wheel_url: str
    sha256: str
    notes: str = ""

    def to_dict(self) -> dict:
        return {"version": self.version, "notes": self.notes}


def _manifest_url(config) -> str:
    """The configured manifest URL, or "" if it is unusable.

    HTTPS is required. The manifest carries the sha256 that gates the wheel
    we install, so serving it over http would let anyone on the path choose
    BOTH the hash and the binary it authorizes - the checksum would then be
    verifying an attacker's file against an attacker's hash.
    """
    url = (getattr(config.settings, "update_url", "") or "").strip()
    if not url:
        return ""
    if not url.lower().startswith("https://"):
        # Loud, not silent: a misconfigured URL should look like a mistake to
        # fix, not like "no updates available".
        import warnings

        warnings.warn(
            f"update_url must be https:// (got {url.split(':', 1)[0]}://); "
            f"self-update is disabled until it is corrected.", stacklevel=2)
        return ""
    return url


def check(config) -> Available | None:
    """Is a newer build published? None when up to date, unconfigured, or
    unreachable - a failed check must never block or alarm."""
    url = _manifest_url(config)
    if not url:
        return None
    try:
        import httpx

        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            data = c.get(url).json()
    except Exception:
        return None
    try:
        version = str(data["version"]).strip()
        wheel_url = str(data["wheel_url"]).strip()
        sha = str(data["sha256"]).strip().lower()
    except (KeyError, TypeError, AttributeError):
        return None
    if not version or not wheel_url or len(sha) != 64:
        return None
    if parse_version(version) <= parse_version(current_version()):
        return None
    if not wheel_url.lower().startswith("https://"):
        return None          # never fetch an app build over plaintext
    return Available(version=version, wheel_url=wheel_url, sha256=sha,
                     notes=str(data.get("notes") or "")[:500])


def stage(config, avail: Available | None = None) -> dict:
    """Download and verify the new wheel, then park it for the next launch."""
    avail = avail or check(config)
    if avail is None:
        return {"staged": False, "reason": "already up to date"}
    d = _updates_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    name = wheel_name(avail.version, avail.wheel_url)
    tmp = d / (name + ".part")
    try:
        import httpx

        with httpx.Client(timeout=60, follow_redirects=True) as c:
            with c.stream("GET", avail.wheel_url) as resp:
                resp.raise_for_status()
                h = hashlib.sha256()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(65536):
                        h.update(chunk)
                        f.write(chunk)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return {"staged": False, "reason": f"download failed: {e}"}
    if h.hexdigest() != avail.sha256:
        # A wrong hash is the one case worth being loud about: it means the
        # bytes are not what the publisher signed off on.
        tmp.unlink(missing_ok=True)
        return {"staged": False, "reason": "the download did not match its "
                                           "checksum and was discarded"}
    for stale in d.glob("*.whl"):
        stale.unlink(missing_ok=True)     # only one staged build at a time
    os.replace(tmp, d / name)
    (d / PENDING).write_text(json.dumps({
        "version": avail.version, "sha256": avail.sha256, "file": name,
        "notes": avail.notes, "from": current_version(),
    }, indent=2), encoding="utf-8")
    return {"staged": True, "version": avail.version}


def staged_wheel(config, info: dict) -> Path | None:
    """The staged file for a pending record, tolerating an older stage that
    predates the filename being recorded."""
    d = _updates_dir(config)
    name = str(info.get("file") or "")
    if name and set(name) <= _WHEEL_CHARS and ".." not in name:
        p = d / name
        if p.exists():
            return p
    found = sorted(d.glob("*.whl"))
    return found[0] if found else None


def pending(config) -> dict | None:
    p = _updates_dir(config) / PENDING
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if staged_wheel(config, info) else None


def _app_python() -> str:
    """The interpreter the INSTALLED app runs from.

    Usually sys.executable, but not on Windows: launch.py is started by the
    signed base python and puts the venv on sys.path itself, so sys.prefix
    points at the base install. Discover the venv the same way launch.py
    does, from the directory holding config.toml/launch.py.
    """
    here = Path(__file__).resolve()
    roots = []
    cfg = os.environ.get("BRAIN_CONFIG")
    if cfg:
        roots.append(Path(cfg).resolve().parent)
    roots.append(here.parents[2])          # a source checkout / installed app
    for root in roots:
        for venv in (".venv", "venv"):
            for exe in (root / venv / "Scripts" / "python.exe",
                        root / venv / "bin" / "python"):
                if exe.exists():
                    return str(exe)
    return sys.executable


def _installer_cmd(wheel: Path) -> list[str] | None:
    """How to install a wheel into THIS environment.

    The two installers build the venv differently and this has to cope with
    both: the Mac one runs `uv venv` with no --seed, so there is NO pip in
    that venv and its bundled uv is the only option; the Windows one uses
    stdlib venv, which does have pip.
    """
    # Resolve the APP's interpreter, not necessarily the running one. On
    # Windows the shortcut deliberately launches the signed base python and
    # adds the venv to sys.path, so sys.prefix is the BASE install - keying
    # off it installed every update into the wrong environment, leaving the
    # app on old code with no error.
    target = _app_python()
    prefix = Path(target).resolve().parent.parent
    for uv in (prefix.parent / "bin" / "uv", prefix.parent / "bin" / "uv.exe",
               prefix / "bin" / "uv", prefix / "Scripts" / "uv.exe"):
        if uv.exists():
            # --reinstall-package: the version may be unchanged between
            # builds, and uv treats a same-version wheel as already
            # satisfied, so without this an "update" installs nothing.
            return [str(uv), "pip", "install", "--python", target,
                    "--reinstall-package", "schoolhub", str(wheel)]
    try:
        import pip  # noqa: F401
    except Exception:
        return None
    return [target, "-m", "pip", "install", "--force-reinstall",
            "--no-deps", str(wheel)]


def apply_pending(config, *, log=print) -> dict:
    """Install a staged wheel. Called at LAUNCH, before anything is served.

    Returns {applied, version|reason}. Never raises: a failed update must
    leave the working app running, not stop it from starting.
    """
    info = pending(config)
    if not info:
        return {"applied": False, "reason": "nothing staged"}
    d = _updates_dir(config)
    wheel = staged_wheel(config, info)
    if wheel is None:
        return {"applied": False, "reason": "the staged file is missing"}
    try:
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    except OSError as e:
        return {"applied": False, "reason": f"could not read the update: {e}"}
    if digest != info.get("sha256"):
        wheel.unlink(missing_ok=True)
        (d / PENDING).unlink(missing_ok=True)
        return {"applied": False, "reason": "staged update failed its checksum"}
    cmd = _installer_cmd(wheel)
    if cmd is None:
        return {"applied": False,
                "reason": "no installer available in this environment"}
    log(f"[update] installing {info.get('version')}...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"applied": False, "reason": f"install failed: {e}"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        # Leave the staged file alone: the next launch can retry, and the
        # app keeps running the version that works in the meantime.
        return {"applied": False, "reason": f"install failed: {tail}"}
    wheel.unlink(missing_ok=True)
    (d / PENDING).unlink(missing_ok=True)
    (d / "last_applied.json").write_text(json.dumps(info, indent=2),
                                         encoding="utf-8")
    log(f"[update] now on {info.get('version')}")
    return {"applied": True, "version": info.get("version")}
