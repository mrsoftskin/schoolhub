# launch.py - Command Center double-click launcher (Windows + macOS).
#
# Design (from the installer research):
#   * NO console window: Windows runs it under pythonw.exe (GUI subsystem);
#     macOS launches it from an .app bundle, which never shows a Terminal.
#   * Host uvicorn IN-PROCESS (no detached child) so nothing unsigned is spawned;
#     the only process is the signed pythonw. Sidesteps Smart App Control's
#     scrutiny of pip/uv launcher .exes.
#   * Friendly, non-frozen startup via a tiny Tk control window.
#   * Clean stop: close the window / click Quit -> server.should_exit.
#
# The installer points a Start-Menu/Desktop shortcut at:
#   Target : <signed Python 3.12>\pythonw.exe
#   Args   : "<AppHome>\launch.py"
#   StartIn: <AppHome>
# We launch the BASE signed pythonw and add the venv's site-packages to sys.path
# ourselves, so we never depend on the venv's copied .exe being SAC-trusted.

import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent

# --- config: create_app() reads BRAIN_CONFIG; point it at our own config.toml
#     so discovery never depends on the working directory. ---
_cfg = APP / "config.toml"
if _cfg.exists():
    os.environ.setdefault("BRAIN_CONFIG", str(_cfg))

# --- pythonw has sys.stdout/stderr == None; uvicorn logs on the first line and
#     would crash. Redirect to a log the friend can send us, BEFORE any import. ---
LOG_DIR = APP / "data" / "logs"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log = open(LOG_DIR / "server.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = _log
    sys.stderr = _log
except Exception:
    _null = open(os.devnull, "w")
    sys.stdout = sys.stderr = _null

# --- import path: dev src/ if present, plus the venv's site-packages (works
#     whether launched by the base interpreter or the venv's own python). ---
sys.path.insert(0, str(APP / "src"))
for _venv in (".venv", "venv"):
    # Windows puts site-packages at <venv>/Lib/site-packages; POSIX (macOS,
    # Linux) uses <venv>/lib/python3.X/site-packages, so the layout must be
    # discovered, not hardcoded - the Windows-only path made the launcher
    # import nothing on a Mac.
    _candidates = [APP / _venv / "Lib" / "site-packages"]
    _candidates += sorted((APP / _venv / "lib").glob("python3.*/site-packages"))
    for _sp in _candidates:
        if _sp.is_dir() and str(_sp) not in sys.path:
            sys.path.append(str(_sp))

# --- defensive: stub the legacy-Windows-console shim so click/typer never
#     reach for a console we do not have under pythonw. ---
import types
for _name in ("click._winconsole", "typer._click._winconsole"):
    _m = types.ModuleType(_name)
    _m._get_windows_console_stream = lambda *a, **k: None
    sys.modules[_name] = _m

HOST = "127.0.0.1"
# Default 8177; a test run can set CC_PORT to avoid colliding with a live app.
try:
    PORT = int(os.environ.get("CC_PORT", "8177"))
except ValueError:
    PORT = 8177
URL = f"http://{HOST}:{PORT}"


def _server_up() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((HOST, PORT)) == 0


def _open_browser() -> None:
    import webbrowser
    webbrowser.open(URL)


def _apply_staged_update() -> None:
    """Install a downloaded update before anything is served.

    This is the only safe moment: once uvicorn is up, the app is importing
    from the very package an update would replace, and a half-swapped
    install is worse than a stale one. Best-effort - a failed update must
    never stop the app from starting.
    """
    try:
        from brain.config import find_config, load_config
        from brain import updates

        cfg = load_config(find_config(os.environ.get("BRAIN_CONFIG")))
        if updates.pending(cfg):
            res = updates.apply_pending(cfg)
            print(f"[launcher] update: {res}", flush=True)
    except Exception as e:
        print(f"[launcher] update check skipped: {e}", flush=True)


def _index_and_calendar() -> bool:
    """Index new course files and refresh the calendar. Returns False if
    either failed, so the UI can say so instead of reporting "Ready".

    The calendar import belongs here because nothing else ever called it:
    a friend who pasted their Google Calendar link during setup would have
    seen an empty calendar forever, which reads as "the link was wrong".
    """
    ok = True
    try:
        from brain.core import Core

        core = Core.load(os.environ.get("BRAIN_CONFIG"))
    except Exception as e:
        print(f"[launcher] could not load the config: {e}", flush=True)
        return False
    try:
        core.index()
    except Exception as e:
        print(f"[launcher] indexing failed: {type(e).__name__}: {e}", flush=True)
        ok = False
    try:
        if core.config.calendar:
            core.calendar_import()
    except Exception as e:
        print(f"[launcher] calendar import failed: {type(e).__name__}: {e}",
              flush=True)
        ok = False
    return ok


def _run_headless(server) -> int:
    """No Tk available: wait for the server, open the browser, then block.
    Ctrl-C (or quitting the app) shuts the server down cleanly."""
    import time

    for _ in range(120):
        if _server_up():
            break
        time.sleep(0.25)
    _open_browser()
    _index_and_calendar()
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


def main() -> int:
    # Single-instance: a second double-click just re-opens the browser instead
    # of trying (and failing) to bind the port again.
    if _server_up():
        _open_browser()
        return 0

    # Before uvicorn imports anything from the package an update replaces.
    _apply_staged_update()

    import queue
    import threading

    import uvicorn

    config = uvicorn.Config("brain.web.app:create_app", host=HOST, port=PORT,
                            factory=True, log_level="info")
    server = uvicorn.Server(config)

    def _run_server():
        try:
            server.run()
        except Exception:
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run_server, daemon=True).start()

    # Tk is optional: the status window is a nicety, not the app. Some Python
    # builds (notably slim/standalone ones on macOS and Linux) ship without
    # tkinter, and the app must still run there - serve, open the browser, and
    # stay up until the user closes the window or quits the process.
    try:
        import tkinter as tk
    except Exception:
        return _run_headless(server)

    # Importing tkinter is not the same as being able to OPEN a window: a
    # standalone Python with a broken Aqua Tcl, or no window server, raises
    # here. Unguarded that killed the whole app with no message at all - the
    # Dock icon bounced once and vanished. Falling back keeps it usable.
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"[launcher] no window available ({e}); running without one",
              flush=True)
        return _run_headless(server)
    root.title("Command Center")
    root.geometry("380x160")
    root.resizable(False, False)
    # .ico is a Windows format; macOS/Linux Tk raise here and keep the
    # default icon, which is fine.
    try:
        root.iconbitmap(str(APP / "assets" / "app.ico"))
    except Exception:
        pass

    status = tk.StringVar(value="Starting Command Center...")
    tk.Label(root, textvariable=status, wraplength=350, justify="center",
             font="TkDefaultFont", pady=16).pack()

    btns = tk.Frame(root)
    btns.pack(pady=6)
    open_btn = tk.Button(btns, text="Open Command Center", state="disabled",
                         width=22, command=_open_browser)
    open_btn.grid(row=0, column=0, padx=6)

    def _quit():
        status.set("Shutting down...")
        server.should_exit = True
        root.after(400, lambda: (root.destroy(), os._exit(0)))

    tk.Button(btns, text="Quit", width=8, command=_quit).grid(row=0, column=1, padx=6)
    root.protocol("WM_DELETE_WINDOW", _quit)

    events: "queue.Queue[str]" = queue.Queue()

    def _wait_ready():
        import time
        for _ in range(120):          # ~30s ceiling; boot is normally 2-5s
            if _server_up():
                events.put("ready")
                return
            time.sleep(0.25)
        events.put("timeout")

    def _prewarm():
        # After the server is up, incrementally index whatever the friend has
        # dropped into materials/ since last time (index is content-hashed, so
        # unchanged files are skipped and this is fast on later launches). This
        # also loads the embedding model from the install-time cache, so the
        # first Chat is instant. No command for the friend to remember.
        events.put("warming")
        ok = _index_and_calendar()
        events.put("warm-done" if ok else "warm-failed")

    opened = {"done": False}

    def _pump():
        try:
            while True:
                ev = events.get_nowait()
                if ev == "ready":
                    status.set("Command Center is running.")
                    open_btn.config(state="normal")
                    if not opened["done"]:
                        opened["done"] = True
                        _open_browser()
                    threading.Thread(target=_prewarm, daemon=True).start()
                elif ev == "warming":
                    status.set("Reading your course files and preparing the AI\n"
                               "(quick after the first time)...")
                elif ev == "warm-done":
                    status.set("Ready. Command Center is running.\n"
                               "Closing this window quits the app.")
                elif ev == "warm-failed":
                    # Saying "Ready" after a failed index is how "no search
                    # results" gets mistaken for an empty library.
                    status.set("Running, but reading your files hit a problem.\n"
                               "Run the checkup (see the guide).")
                elif ev == "timeout":
                    status.set("Still starting... if the app does not open, see\n"
                               "data/logs/server.log")
        except queue.Empty:
            pass
        root.after(120, _pump)

    threading.Thread(target=_wait_ready, daemon=True).start()
    root.after(120, _pump)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
