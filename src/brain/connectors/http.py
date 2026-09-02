"""Thin cookie-replaying HTTP client shared by connectors.

Detects the classic "your session expired" failure: an endpoint that should
return JSON instead 200s with an HTML login page. That is turned into
LoginRequired so sync never imports an empty result as if the user simply had
no assignments.
"""

from __future__ import annotations

from .base import LoginRequired

TIMEOUT = 25.0


def client(session: dict):
    import httpx

    cookies = session.get("cookies") or {}
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, cookies=cookies,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
    )


def get_json(session: dict, url: str, site: str, **params):
    import httpx

    with client(session) as c:
        try:
            resp = c.get(url, params=params or None)
        except Exception as e:  # network
            raise LoginRequired(f"{site}: request failed ({type(e).__name__}: {e})") from e
    ct = resp.headers.get("content-type", "")
    body = resp.text
    if resp.status_code in (401, 403) or "text/html" in ct or _looks_like_login(body):
        raise LoginRequired(
            f"{site}: the saved session was rejected (looks logged out). "
            f"Re-capture it with: brain sync login {site}"
        )
    try:
        return resp.json()
    except Exception as e:
        raise LoginRequired(
            f"{site}: expected JSON from {url} but got {ct or 'unknown'}. "
            f"The endpoint or session may be wrong. Re-capture: brain sync login {site}"
        ) from e


def _looks_like_login(body: str) -> bool:
    head = body[:2000].lower()
    return any(s in head for s in (
        "<html", "<!doctype", "sign in", "log in", "login", "d2l.lp.web.authentication",
    ))


def get_html(session: dict, url: str, site: str) -> str:
    """Fetch an HTML page with the stored session (for sites that embed their
    data server-side rather than exposing JSON). Login detection differs from
    get_json: HTML is EXPECTED here, so being logged out shows up as a
    redirect to an auth host/path or a login form in the body."""
    with client(session) as c:
        try:
            resp = c.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        except Exception as e:
            raise LoginRequired(f"{site}: request failed ({type(e).__name__}: {e})") from e
    final = str(resp.url).lower()
    body = resp.text
    if (resp.status_code in (401, 403)
            or any(s in final for s in ("/login", "/sign_in", "/cas/", "auth"))
            or 'type="password"' in body[:8000].lower()):
        raise LoginRequired(
            f"{site}: the saved session was rejected (looks logged out). "
            f"Re-capture it with: brain sync login {site}"
        )
    if resp.status_code != 200:
        raise LoginRequired(f"{site}: HTTP {resp.status_code} from {url}")
    return body
