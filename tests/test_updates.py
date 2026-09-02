"""Self-update.

Two rules are load-bearing and both are tested hard: an update is never
installed into the RUNNING process (it is staged and applied at next launch,
the only moment nothing is imported yet), and a download is never installed
without matching the checksum the publisher advertised.
"""

from __future__ import annotations

import hashlib

from brain import updates
from brain.config import load_config
from conftest import write_config

WHEEL_BYTES = b"PK\x03\x04 pretend this is a wheel " * 50
SHA = hashlib.sha256(WHEEL_BYTES).hexdigest()


def _cfg(tmp_path, url="https://example.test/manifest.json"):
    p = write_config(tmp_path, [{"name": "FINC313", "assist_level": "full"}])
    text = p.read_text(encoding="utf-8").replace(
        "[settings]", f'[settings]\nupdate_url = "{url}"', 1)
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def _manifest(version="9.9.9", sha=SHA, url="https://example.test/app.whl"):
    return {"version": version, "wheel_url": url, "sha256": sha, "notes": "hi"}


class FakeResponse:
    def __init__(self, payload=None, body=b""):
        self._payload, self._body = payload, body

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None

    def iter_bytes(self, n=65536):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeClient:
    def __init__(self, payload=None, body=b""):
        self._payload, self._body = payload, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return FakeResponse(self._payload, self._body)

    def stream(self, method, url):
        return FakeResponse(self._payload, self._body)


def _patch_http(monkeypatch, payload=None, body=b""):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: FakeClient(payload, body))


# ---- version comparison -------------------------------------------------

def test_version_ordering():
    assert updates.parse_version("0.2.0") > updates.parse_version("0.1.9")
    assert updates.parse_version("1.0") > updates.parse_version("0.9.9")
    assert updates.parse_version("0.1.0") == updates.parse_version("0.1.0")
    # a suffix must not crash or invert the order
    assert updates.parse_version("0.2.0rc1") >= updates.parse_version("0.1.0")
    assert updates.parse_version("") == (0,)


# ---- check --------------------------------------------------------------

def test_no_update_url_means_no_network(tmp_path, monkeypatch):
    """Default is empty: an app must not contact a server nobody configured."""
    cfg = _cfg(tmp_path, url="")

    def explode(*a, **k):
        raise AssertionError("check() hit the network with no update_url")

    import httpx

    monkeypatch.setattr(httpx, "Client", explode)
    assert updates.check(cfg) is None


def test_older_or_equal_version_is_not_an_update(tmp_path, monkeypatch):
    _patch_http(monkeypatch, payload=_manifest(version=updates.current_version()))
    assert updates.check(_cfg(tmp_path)) is None
    _patch_http(monkeypatch, payload=_manifest(version="0.0.1"))
    assert updates.check(_cfg(tmp_path)) is None


def test_newer_version_is_offered(tmp_path, monkeypatch):
    _patch_http(monkeypatch, payload=_manifest())
    avail = updates.check(_cfg(tmp_path))
    assert avail and avail.version == "9.9.9"


def test_plaintext_wheel_url_is_refused(tmp_path, monkeypatch):
    """An app build is executable code; it does not arrive over http://."""
    _patch_http(monkeypatch, payload=_manifest(url="http://example.test/app.whl"))
    assert updates.check(_cfg(tmp_path)) is None


def test_malformed_manifest_is_ignored(tmp_path, monkeypatch):
    bad_manifests = [
        {},
        {"version": "9.9.9"},
        {"version": "9.9.9", "wheel_url": "https://x", "sha256": "tooshort"},
    ]
    for bad in bad_manifests:
        _patch_http(monkeypatch, payload=bad)
        assert updates.check(_cfg(tmp_path)) is None


def test_unreachable_server_is_silent(tmp_path, monkeypatch):
    import httpx

    class Boom:
        def __enter__(self):
            raise OSError("no network")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: Boom())
    assert updates.check(_cfg(tmp_path)) is None


# ---- stage --------------------------------------------------------------

def test_stage_downloads_and_parks_for_next_launch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    res = updates.stage(cfg)
    assert res["staged"] and res["version"] == "9.9.9"
    assert updates.pending(cfg)["version"] == "9.9.9"
    staged = tmp_path / "data" / "updates" / "schoolhub-9.9.9-py3-none-any.whl"
    assert staged.read_bytes() == WHEEL_BYTES


def test_a_corrupt_download_is_discarded_not_installed(tmp_path, monkeypatch):
    """The whole point of the checksum: a truncated or swapped file must
    never reach the installer."""
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=b"not the real wheel")
    res = updates.stage(cfg)
    assert not res["staged"] and "checksum" in res["reason"]
    assert updates.pending(cfg) is None
    assert not list((tmp_path / "data" / "updates").glob("*.whl"))


def test_a_failed_download_leaves_nothing_behind(tmp_path, monkeypatch):
    import httpx

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            raise OSError("connection reset")

        def get(self, *a, **k):
            return FakeResponse(_manifest())

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: Boom())
    cfg = _cfg(tmp_path)
    res = updates.stage(cfg, updates.Available("9.9.9", "https://x/a.whl", SHA))
    assert not res["staged"]
    assert not list((tmp_path / "data" / "updates").glob("*.part"))


# ---- apply --------------------------------------------------------------

def test_apply_runs_the_installer_and_clears_the_stage(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    updates.stage(cfg)

    calls = []

    class Ok:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Ok()

    monkeypatch.setattr(updates, "_installer_cmd", lambda w: ["installer", str(w)])
    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    res = updates.apply_pending(cfg, log=lambda m: None)
    assert res["applied"] and res["version"] == "9.9.9"
    assert calls, "the installer should have been invoked"
    assert updates.pending(cfg) is None


def test_a_failed_install_keeps_the_stage_for_a_retry(tmp_path, monkeypatch):
    """The app must keep running the version that works, and try again next
    launch rather than losing the download."""
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    updates.stage(cfg)

    class Fail:
        returncode, stdout, stderr = 1, "", "no space left on device"

    monkeypatch.setattr(updates, "_installer_cmd", lambda w: ["installer"])
    monkeypatch.setattr(updates.subprocess, "run", lambda cmd, **k: Fail())
    res = updates.apply_pending(cfg, log=lambda m: None)
    assert not res["applied"] and "no space left" in res["reason"]
    assert updates.pending(cfg) is not None      # retried next launch


def test_apply_refuses_a_stage_that_was_tampered_with(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    updates.stage(cfg)
    staged = updates.staged_wheel(cfg, updates.pending(cfg))
    staged.write_bytes(b"swapped")

    def must_not_install(wheel):
        raise AssertionError("must not install a wheel that failed its checksum")

    monkeypatch.setattr(updates, "_installer_cmd", must_not_install)
    res = updates.apply_pending(cfg, log=lambda m: None)
    assert not res["applied"] and "checksum" in res["reason"]
    assert updates.pending(cfg) is None          # and the bad file is gone


def test_apply_with_nothing_staged_is_a_no_op(tmp_path):
    res = updates.apply_pending(_cfg(tmp_path), log=lambda m: None)
    assert res == {"applied": False, "reason": "nothing staged"}


# ---- the staged filename (a real bug, found by running the real path) ----

def test_staged_file_keeps_a_valid_wheel_name():
    """uv reads the version out of the FILENAME and refuses anything else:
    "The wheel filename 'pending.whl' is invalid: Must have a version". uv is
    the ONLY installer present on a Mac, so a generic name broke every Mac
    update while working fine on Windows, where pip is forgiving.
    """
    assert updates.wheel_name("0.2.0") == "schoolhub-0.2.0-py3-none-any.whl"
    # a publisher's own filename is kept
    assert updates.wheel_name(
        "0.2.0", "https://x/y/schoolhub-0.2.0-py3-none-any.whl"
    ) == "schoolhub-0.2.0-py3-none-any.whl"


def test_staged_filename_cannot_escape_the_updates_folder():
    """The name comes from a remote manifest, so it is untrusted input."""
    for hostile in ("https://x/../../evil.whl", "https://x/..%2fevil.whl",
                    "https://x/a b-1-2.whl", "https://x/nodashes.whl"):
        name = updates.wheel_name("0.2.0", hostile)
        assert name == "schoolhub-0.2.0-py3-none-any.whl", hostile
        assert "/" not in name and ".." not in name


def test_stage_writes_the_real_name_and_records_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    updates.stage(cfg)
    info = updates.pending(cfg)
    assert info["file"] == "schoolhub-9.9.9-py3-none-any.whl"
    staged = updates.staged_wheel(cfg, info)
    assert staged.name == info["file"] and staged.read_bytes() == WHEEL_BYTES


def test_staging_a_second_build_replaces_the_first(tmp_path, monkeypatch):
    """Two wheels in the folder would make the fallback pick arbitrarily."""
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(version="9.9.8"), body=WHEEL_BYTES)
    updates.stage(cfg)
    _patch_http(monkeypatch, payload=_manifest(version="9.9.9"), body=WHEEL_BYTES)
    updates.stage(cfg)
    wheels = sorted((tmp_path / "data" / "updates").glob("*.whl"))
    assert len(wheels) == 1 and "9.9.9" in wheels[0].name


def test_pending_ignores_a_record_whose_file_vanished(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_http(monkeypatch, payload=_manifest(), body=WHEEL_BYTES)
    updates.stage(cfg)
    for w in (tmp_path / "data" / "updates").glob("*.whl"):
        w.unlink()
    assert updates.pending(cfg) is None


def test_the_declared_version_matches_the_package_metadata():
    """The version lives in two hand-edited files. If they drift, the update
    manifest advertises a build that does not exist, or advertises nothing at
    all - the whole channel fails silently."""
    import tomllib
    from pathlib import Path

    import brain

    root = Path(brain.__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():          # installed, not a source checkout
        return
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == brain.__version__, (
        f"pyproject says {declared}, brain.__version__ says {brain.__version__}")


def test_a_plaintext_manifest_url_disables_updates(tmp_path, monkeypatch):
    """The manifest carries the sha256 that gates the wheel we install, so
    over http an attacker on the path chooses BOTH the hash and the binary
    it authorizes - the checksum would verify their file against their hash.
    """
    import warnings

    cfg = _cfg(tmp_path, url="http://example.test/manifest.json")

    def explode(*a, **k):
        raise AssertionError("fetched a manifest over plaintext http")

    import httpx

    monkeypatch.setattr(httpx, "Client", explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert updates.check(cfg) is None
    assert any("https" in str(w.message) for w in caught), \
        "a misconfigured URL must be loud, not silently 'no updates'"


def test_the_installer_targets_the_apps_own_interpreter(tmp_path, monkeypatch):
    """On Windows the launcher runs the SIGNED BASE python and adds the venv
    to sys.path itself, so sys.prefix is the base install. Keying the install
    off it put every update in the wrong environment and left the app on old
    code with no error."""
    app = tmp_path / "AppHome"
    (app / ".venv" / "Scripts").mkdir(parents=True)
    exe = app / ".venv" / "Scripts" / "python.exe"
    exe.write_text("", encoding="utf-8")
    (app / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("BRAIN_CONFIG", str(app / "config.toml"))
    assert updates._app_python() == str(exe)
