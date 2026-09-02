"""Assemble the friend-facing installer zips (Windows and macOS).

Run from the repo root:  uv run python packaging/make_release.py

Produces TWO zips in dist/, each containing everything that platform's friend
needs - the app wheel, pinned library versions, the browser extension, the
launcher, the install script, and a guide:
    CommandCenter-Setup.zip      Windows (double-click install.bat)
    CommandCenter-Setup-Mac.zip  macOS   (one line pasted into Terminal)
Ships NONE of your personal data (config, data/, materials/, .venv stay out).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
PKG = REPO / "packaging"
DIST = REPO / "dist"
STAGE = DIST / "CommandCenter-Setup"


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO, check=True)


# Text files are authored on Windows, so they can pick up CRLF endings. A shell
# script with CRLF does not merely look untidy on macOS: /bin/bash treats the
# carriage return as part of the word, so `case ... in\r` and `then\r` are not
# recognized and install.sh dies at parse time having created nothing. Git Bash
# silently strips CR, so `bash -n` ON WINDOWS reports a clean syntax check -
# which is exactly how a broken installer ships unnoticed.
_TEXT_SUFFIXES = {".sh", ".txt", ".py", ".bat", ".ps1", ".md", ".json"}


def _copy_text(src: Path, dest: Path) -> None:
    """Copy a file, forcing LF endings for anything a POSIX shell will read."""
    data = src.read_bytes()
    if src.suffix.lower() in _TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    dest.write_bytes(data)


def _assert_lf(stage: Path) -> None:
    """Refuse to ship a staged text file that still carries CRLF."""
    bad = [p for p in sorted(stage.rglob("*"))
           if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES
           and b"\r\n" in p.read_bytes()]
    if bad:
        names = ", ".join(str(p.relative_to(stage)) for p in bad)
        raise SystemExit(
            f"ERROR: CRLF line endings in staged file(s): {names}. "
            f"These break on macOS/Linux. Fix _copy_text before releasing."
        )


def _assert_guide_paths(stage: Path) -> None:
    """The guides quote venv paths the installers create; they drifted.

    packaging/GUIDE.txt told every Windows friend to run
    `...\SchoolHub\venv\Scripts\python.exe` while install.ps1 creates
    `.venv`, so BOTH documented recovery commands failed for everyone. A
    support path nobody can execute is worse than none, so the build refuses
    to ship a guide whose paths do not match its installer.
    """
    checks = [
        ("GUIDE.txt", "install.ps1", "\.venv\\", "\venv\\"),
        ("GUIDE.txt", "install.sh", "/venv/", None),
    ]
    guide = stage / "GUIDE.txt"
    if not guide.exists():
        return
    text = guide.read_text(encoding="utf-8", errors="replace")
    for _g, installer, want, forbid in checks:
        if not (stage / installer).exists():
            continue
        if forbid and forbid in text and want not in text:
            raise SystemExit(
                f"ERROR: {stage.name}/GUIDE.txt quotes {forbid!r} but "
                f"{installer} creates {want!r}. Fix the guide before releasing."
            )


def main() -> int:
    DIST.mkdir(exist_ok=True)

    # 1. Fresh wheel + pinned lockfile. Clear old wheels first: the build
    # used to pick `sorted(dist/*.whl)[-1]`, a STRING sort over a directory
    # nothing cleaned, so 0.10.0 would sort below 0.9.0 and silently ship old
    # code under a correct-looking manifest.
    for stale in DIST.glob("schoolhub-*.whl"):
        stale.unlink()
    run(["uv", "build", "--wheel"])
    # --no-hashes: versions stay pinned (==), but without per-file hashes pip
    # does NOT enter --require-hashes mode, so the local app wheel (which has no
    # PyPI hash) installs in the same command as the pinned dependencies.
    lock = REPO / "requirements-lock.txt"
    with open(lock, "w", encoding="utf-8") as f:
        subprocess.run(["uv", "export", "--no-dev", "--format", "requirements-txt",
                        "--no-emit-project", "--no-hashes"], cwd=REPO, check=True, stdout=f)

    # A second, platform-specific pin set for Intel Macs: a few packages
    # (onnxruntime, cryptography) publish x86_64 macOS wheels only on older
    # releases, so resolving for that platform is what makes older Macs work.
    intel_lock = REPO / "requirements-lock-macos-intel.txt"
    # Relative paths on purpose: uv records the full command line in the
    # lockfile header, and absolute ones would publish the build machine's
    # username in a file that ships to every friend and to the repo.
    subprocess.run(["uv", "pip", "compile", "pyproject.toml",
                    "--python-platform", "x86_64-apple-darwin",
                    "--python-version", "3.12",
                    "--constraints", "packaging/mac/intel-constraints.txt",
                    "-o", "requirements-lock-macos-intel.txt"], cwd=REPO,
                   check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

    wheels = list(DIST.glob("schoolhub-*.whl"))
    if len(wheels) != 1:
        print(f"ERROR: expected exactly one freshly built wheel, found "
              f"{[w.name for w in wheels]}", file=sys.stderr)
        return 1
    wheel = wheels[0]
    # The wheel must be the version the source claims, or the manifest would
    # advertise a build nobody can install.
    import brain

    if f"-{brain.__version__}-" not in wheel.name:
        print(f"ERROR: built {wheel.name} but brain.__version__ is "
              f"{brain.__version__}; bump both and rebuild.", file=sys.stderr)
        return 1

    # 2. Stage + zip each platform.
    def build(stage_name: str, extras: list[tuple[Path, str]]) -> Path:
        stage = DIST / stage_name
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        # Shared payload: the app, the exact library versions, the launcher,
        # and the browser extension (which travels verbatim).
        shutil.copy2(wheel, stage / wheel.name)
        _copy_text(lock, stage / "requirements-lock.txt")
        _copy_text(REPO / "launch.py", stage / "launch.py")
        shutil.copytree(REPO / "browser-extension", stage / "browser-extension",
                        ignore=shutil.ignore_patterns("*.map", ".DS_Store"))
        for src, dest in extras:
            _copy_text(src, stage / dest)
        _assert_lf(stage)
        _assert_guide_paths(stage)
        zpath = DIST / (stage_name + ".zip")
        if zpath.exists():
            zpath.unlink()
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(stage.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(stage.parent))
        return zpath

    zips = [
        build("CommandCenter-Setup", [
            (PKG / "install.bat", "install.bat"),
            (PKG / "install.ps1", "install.ps1"),
            (PKG / "GUIDE.txt", "GUIDE.txt"),
        ]),
        build("CommandCenter-Setup-Mac", [
            (PKG / "mac" / "install.sh", "install.sh"),
            (PKG / "mac" / "GUIDE-mac.txt", "GUIDE.txt"),
            (intel_lock, "requirements-lock-macos-intel.txt"),
        ]),
    ]

    # 3. The update manifest. Hand-maintaining a SHA-256 is exactly the kind
    # of chore that gets skipped, and a wrong hash means every friend's update
    # is refused - so it is generated from the wheel that was just built.
    import hashlib

    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    version = wheel.name.split("-")[1]
    manifest = DIST / "update-manifest.json"
    manifest.write_text(json.dumps({
        "version": version,
        # Point this at the Release asset URL once the repo exists. The app
        # refuses a non-https URL, so leave the scheme alone.
        "wheel_url": f"https://REPLACE-ME/releases/latest/download/{wheel.name}",
        "sha256": sha,
        "notes": "",
    }, indent=2), encoding="utf-8")
    print(f"\nWrote {manifest}")
    print(f"  version {version}  sha256 {sha[:16]}...")
    print("  Set wheel_url to the Release asset URL, publish this file at a")
    print("  stable https URL, and point [settings] update_url at it.")

    for zip_path in zips:
        size_mb = zip_path.stat().st_size / 1e6
        print(f"\nBuilt {zip_path}  ({size_mb:.2f} MB)")
        print("Contents:")
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                print("   ", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
