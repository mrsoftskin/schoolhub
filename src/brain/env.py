"""Load secrets from a local .env file into the environment.

The spec's rule is that ANTHROPIC_API_KEY comes from the environment and is
never in code - a .env file honors that: it is environment data, kept out of
source control, owned by the user. Real environment variables always win, so
an already-exported key is never overridden.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = ".env"


def load_env_file(start: str | Path | None = None) -> Path | None:
    """Find a .env beside the config (or walking up from cwd) and load it.

    Returns the file it loaded, or None. Malformed lines are skipped rather
    than raising: a broken .env must not stop the whole app from starting.
    """
    here = Path(start).resolve() if start else Path.cwd().resolve()
    if here.is_file():
        here = here.parent
    for candidate in [here, *here.parents]:
        path = candidate / ENV_FILE
        if path.exists():
            _apply(path)
            return path
    return None


def _apply(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
