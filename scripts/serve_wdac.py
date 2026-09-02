"""Start brain serve when Smart App Control blocks the venv interpreter.

As of 2026-08-27 SAC/WDAC blocks .venv/Scripts/python.exe (a copied,
unsigned uv-standalone exe) and the toolchain's _ctypes extension DLL.
The uv BASE interpreter still runs, and the only ctypes user on the
server boot path is click/typer's legacy-Windows-console shim, which is
safely stubbed (None = "not a legacy console").

Run with:

  $env:PYTHONPATH = "C:\\Users\\<you>\\SchoolHub\\src;C:\\Users\\<you>\\SchoolHub\\.venv\\Lib\\site-packages"
  & "C:\\Users\\<you>\\AppData\\Roaming\\uv\\python\\cpython-3.12-windows-x86_64-none\\python.exe" scripts\\serve_wdac.py

PYTHONPATH must carry src + venv site-packages because the editable .pth
is only processed for true site dirs. Known limit: anything that imports
torch (embeddings for ask/search) still needs ctypes and will fail until
the policy is fixed; the dashboard, calendar, grades, and sync all work.
"""
import sys
import types

for name in ("click._winconsole", "typer._click._winconsole"):
    fake = types.ModuleType(name)
    fake._get_windows_console_stream = lambda *a, **k: None
    sys.modules[name] = fake

from uvicorn.config import Config
from uvicorn.server import Server

print("Command Center on http://127.0.0.1:8177 (WDAC workaround launcher)")
Server(Config("brain.web.app:create_app", host="127.0.0.1", port=8177,
              factory=True)).run()
