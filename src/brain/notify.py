"""Desktop notifications for background-sync findings (Windows + macOS).

Fire-and-forget: a notification failure must never break the poller, so every
path swallows errors after recording them. Uses the WinRT toast API through a
PowerShell subprocess - no extra Python dependencies, verified working on this
machine 2026-08-25.

SECURITY: toast text includes assignment/announcement titles, which are
instructor-authored (untrusted). Neither backend ever interpolates that text
into source. Windows: the .ps1 is a CONSTANT string (a double-quoted here-
string would interpolate `$(...)` into code execution); the text travels as an
XML file, escaped in Python, whose path is passed as an argument. macOS: the
AppleScript is likewise constant and the text arrives through `on run argv`,
so quotes or backslashes in a title can never close the string and inject.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# Constant script: reads the toast XML from the file named by its first
# argument. Nothing user- or instructor-controlled is ever formatted in.
_SCRIPT = """\
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([System.IO.File]::ReadAllText($args[0]))
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Command Center").Show($toast)
"""

_XML_TEMPLATE = """\
<toast><visual><binding template="ToastGeneric">
<text>{title}</text>
<text>{body}</text>
</binding></visual></toast>
"""


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


SUPPORTED = sys.platform in ("win32", "darwin")

# macOS: constant AppleScript; the untrusted title/body arrive as `on run` args,
# never spliced into the script text.
_OSA_SCRIPT = """\non run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
"""


def _toast_macos(title: str, body: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", _OSA_SCRIPT, title[:80], body[:200]],
            capture_output=True, timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def _toast_windows(title: str, body: str) -> bool:
    """Show a Windows toast. Returns False (never raises) on any failure."""
    xml = _XML_TEMPLATE.format(title=_xml_escape(title[:80]),
                               body=_xml_escape(body[:200]))
    ps1_path = xml_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8") as f:
            f.write(_SCRIPT)
            ps1_path = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                         encoding="utf-8") as f:
            f.write(xml)
            xml_path = f.name
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1_path, xml_path],
            capture_output=True, timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False
    finally:
        for p in (ps1_path, xml_path):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass


def toast(title: str, body: str) -> bool:
    """Show a desktop notification. Returns False (never raises) on failure.

    Returns True on platforms with no supported notifier (nothing was asked
    for, so nothing failed) - otherwise a Linux user's sync status would carry
    a permanent "notification failed" line.
    """
    if sys.platform == "win32":
        return _toast_windows(title, body)
    if sys.platform == "darwin":
        return _toast_macos(title, body)
    return True
