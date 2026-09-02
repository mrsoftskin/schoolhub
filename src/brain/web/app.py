"""FastAPI web app. Thin caller over brain.core - route handlers marshal
requests and responses; retrieval, gating, and calendar logic live in the
library. Localhost only; no auth by design.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import analytics
from .. import calendar as cal
from .. import conversations as convo
from .. import weather
from ..ask import GLOBAL_COLLECTION
from ..core import Core
from ..errors import (
    AssistBlocked,
    BrainError,
    EmptyIndexError,
    MissingAPIKeyError,
    NoRelevantResults,
    StoreOutOfSync,
)

STATIC_DIR = Path(__file__).parent / "static"

# Answers are model-generated and the model reads untrusted indexed files, so
# a prompt injection can try to emit markup that phones home. DOMPurify strips
# scripts and event handlers but NOT passive remote loads (img/style url()/
# audio/video/svg <image>), which are enough to exfiltrate retrieved context
# through a URL. This policy blocks every off-origin fetch at the browser.
# marked and DOMPurify are vendored under static/vendor, so no off-origin
# script is needed at all.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "form-action 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])


ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Image upload limits. The client already downscales screenshots, but the
# server never trusts that: it re-validates type, count, and size so a crafted
# request cannot push an oversized or non-image payload to the model.
ALLOWED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif",
})
MAX_IMAGES = 8
MAX_IMAGE_B64 = 8 * 1024 * 1024        # ~6 MB decoded, per image
MAX_IMAGES_B64_TOTAL = 24 * 1024 * 1024


def _parse_images(payload: dict) -> list[dict]:
    """Validate and normalize payload['images'] into [{media_type, data}].

    data is base64 with no data: URI prefix. Raises HTTPException on anything
    malformed - fail loud, never silently drop an attachment the user sent.
    """
    raw = payload.get("images")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "images must be a list")
    if len(raw) > MAX_IMAGES:
        raise HTTPException(400, f"Too many images (max {MAX_IMAGES})")
    out: list[dict] = []
    total = 0
    for i, im in enumerate(raw):
        if not isinstance(im, dict):
            raise HTTPException(400, f"images[{i}] must be an object")
        media_type = im.get("media_type")
        data = im.get("data")
        if media_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                400, f"images[{i}]: unsupported media_type {media_type!r}")
        if not isinstance(data, str) or not data:
            raise HTTPException(400, f"images[{i}]: data must be non-empty base64")
        if len(data) > MAX_IMAGE_B64:
            raise HTTPException(413, f"images[{i}] is too large")
        total += len(data)
        if total > MAX_IMAGES_B64_TOTAL:
            raise HTTPException(413, "Attachments exceed the total size limit")
        out.append({"media_type": media_type, "data": data})
    return out


def _hostname(host_header: str) -> str:
    """Hostname from a Host header, minus any port and IPv6 brackets."""
    host = host_header.strip()
    if host.startswith("["):                      # [::1] or [::1]:8177
        return host[1:].split("]")[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def create_app() -> FastAPI:
    core = Core.load(os.environ.get("BRAIN_CONFIG"))
    app = FastAPI(title="Command Center", docs_url=None, redoc_url=None)
    app.state.core = core

    # Background assignment-sync poller (dry run only; applying stays manual).
    from ..sync_daemon import SyncPoller

    poller = SyncPoller(core, core.config.settings.sync_poll_minutes)
    app.state.sync_poller = poller

    @app.on_event("startup")
    def _start_poller() -> None:
        poller.start()

    @app.on_event("shutdown")
    def _stop_poller() -> None:
        poller.stop()

    # Localhost-only by design. The Host allowlist is what stops a DNS-rebind
    # page from reaching this app as if it were same-origin.
    #
    # Starlette's TrustedHostMiddleware is deliberately not used: it derives
    # the host as host_header.split(":")[0], which turns "[::1]:8177" into
    # "[", so an IPv6 loopback entry can never match and serving on ::1
    # rejects every request. This parses the header properly instead.
    # Extension origins are exempt from the same-origin rule below: a web
    # page cannot forge them, and the session-push endpoint's own header
    # gate stays as its second factor.
    _EXT_SCHEMES = ("chrome-extension://", "moz-extension://",
                    "safari-web-extension://")

    @app.middleware("http")
    async def guard_and_harden(request, call_next):
        if _hostname(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return PlainTextResponse("Invalid host header", status_code=400)
        # State-changing methods refuse cross-origin browser requests: a
        # browser attaches Origin to every POST/DELETE, so same-origin pages
        # send our own host, a malicious page sends its own, and header-less
        # callers (curl, the CLI) pass. Without this, any open web page
        # could fire credentialed scrapes or calendar writes at localhost.
        if request.method in ("POST", "DELETE", "PUT", "PATCH"):
            origin = request.headers.get("origin", "")
            if origin and not origin.startswith(_EXT_SCHEMES):
                parts = origin.split("//", 1)
                if len(parts) != 2 or _hostname(parts[1]) not in ALLOWED_HOSTS:
                    return PlainTextResponse("Cross-origin request refused",
                                             status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ---- state / library ----------------------------------------------

    @app.get("/api/state")
    def state() -> dict:
        conn = core.open_db()
        try:
            stats = core.collection_stats(conn)
        finally:
            conn.close()
        return {
            "collections": stats,
            "models": core.config.settings.models,
            "default_model": core.config.settings.default_model,
            "similarity_floor": core.config.settings.similarity_floor,
            "context_token_budget": core.config.settings.context_token_budget,
            "warnings": core.config.warnings,
            "has_calendar": core.config.calendar is not None,
            # backend_status() already checks the right key for the active
            # backend (Anthropic/OpenAI/Gemini, or the keyless subscription);
            # reuse it rather than hardcoding one vendor's variable.
            "api_key_set": core.backend_status()[0],
            "backend": core.config.settings.backend,
            "backend_ready": core.backend_status()[0],
            "backend_problem": core.backend_status()[1],
            "user": {
                "name": core.config.user.name,
                "location": core.config.user.location_label,
                "has_location": core.config.user.has_location,
            },
        }

    @app.get("/api/library")
    def library() -> dict:
        conn = core.open_db()
        try:
            stats = core.collection_stats(conn)
        finally:
            conn.close()
        calendar_sources = []
        if core.config.calendar:
            from .. import feeds

            for url in core.config.calendar.ics_urls:
                cached = feeds.cache_path(core.config.settings.data_dir, url)
                calendar_sources.append({
                    "type": "feed", "path": url, "exists": cached.exists(),
                })
            for p in core.config.calendar.ics_paths:
                calendar_sources.append({"type": "ics", "path": str(p), "exists": p.exists()})
            if core.config.calendar.fixed_csv:
                p = core.config.calendar.fixed_csv
                calendar_sources.append({"type": "csv", "path": str(p), "exists": p.exists()})
            calendar_sources.append({
                "type": "recurring", "path": f"{len(core.config.calendar.recurring)} rules in config.toml",
                "exists": True,
            })
        return {
            "collections": stats,
            "calendar_sources": calendar_sources,
            "calendar_status": core.calendar_status(),
        }

    # One index run at a time, whichever route starts it. Two runs sharing
    # the embedding store interleave their reads and writes of the same
    # matrix, and the loser silently overwrites the winner.
    _index_job: dict = {
        "running": False, "started": None, "lines": [], "done": False,
        "error": None, "report": None, "collection": None, "force": False,
        "finished": None,
    }
    _index_lock = threading.Lock()
    _MAX_PROGRESS_LINES = 400

    def _index_payload(report) -> dict:
        return {
            "collections": [
                {
                    "collection": c.collection, "scanned": c.scanned, "indexed": c.indexed,
                    "skipped": c.skipped, "removed": c.removed, "chunks_added": c.chunks_added,
                    "failures": [{"path": f.path, "reason": f.reason} for f in c.failures],
                }
                for c in report.collections
            ]
        }

    def _index_status(brief: bool = False) -> dict:
        with _index_lock:
            started = _index_job["started"]
            finished = _index_job["finished"]
            lines = list(_index_job["lines"])
            # Freeze elapsed at the finish. Without this it kept counting
            # after the run ended, so a payload from a completed run was
            # indistinguishable from one two seconds old and a poller could
            # not tell a stale result from a live one.
            end = finished if finished is not None else time.time()
            out = {
                "running": _index_job["running"],
                "done": _index_job["done"],
                "error": _index_job["error"],
                "report": _index_job["report"],
                "collection": _index_job["collection"],
                "started_at": started,
                "finished_at": finished,
                "elapsed_sec": round(end - started, 1) if started else None,
                # The last line is the one worth showing: the indexer emits
                # "  embedded N/TOTAL" as it goes, which is a real fraction.
                "message": lines[-1].strip() if lines else "",
            }
        # lines[] is up to 400 entries (~24 KB) and a 12-minute run gets
        # polled hundreds of times. Callers that only render `message` ask
        # for brief and get a couple of hundred bytes instead.
        if not brief:
            out["lines"] = lines
        return out

    def _run_index(only, force) -> None:
        def say(msg: str) -> None:
            with _index_lock:
                lines = _index_job["lines"]
                lines.append(msg)
                if len(lines) > _MAX_PROGRESS_LINES:
                    # Keep the head (what was set up) and the tail (where it
                    # is now). The middle is one line per indexed file.
                    del lines[20:len(lines) - (_MAX_PROGRESS_LINES - 20)]
        try:
            report = core.index(only=only, force=force, progress=say)
            payload = _index_payload(report)
            with _index_lock:
                _index_job["report"] = payload
        except Exception as e:
            # Any failure, not just BrainError: this runs on a worker thread,
            # so an unhandled exception would vanish with the thread and the
            # status would report "running" forever.
            with _index_lock:
                _index_job["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _index_lock:
                _index_job["running"] = False
                _index_job["done"] = True
                _index_job["finished"] = time.time()
    @app.post("/api/index")
    def reindex(payload: dict = Body(default={})) -> dict:
        only = payload.get("collection")
        force = bool(payload.get("force", False))
        if only and only not in core.config.collection_names():
            raise HTTPException(404, f"Unknown collection '{only}'")
        with _index_lock:
            if _index_job["running"]:
                raise HTTPException(409, "An index run is already in progress.")
            _index_job.update(running=True, started=time.time(), lines=[],
                              done=False, error=None, report=None,
                              finished=None,
                              collection=only, force=force)
        try:
            report = core.index(only=[only] if only else None, force=force)
        except BrainError as e:
            with _index_lock:
                _index_job["error"] = str(e)
            raise HTTPException(500, str(e))
        finally:
            with _index_lock:
                _index_job["running"] = False
                _index_job["done"] = True
                _index_job["finished"] = time.time()
        payload = _index_payload(report)
        # Record it here too, so a status poll after a synchronous run shows
        # the same finished state a background run leaves behind rather than
        # "done, with no report" - which reads as an index that found nothing.
        with _index_lock:
            _index_job["report"] = payload
        return payload

    # ---- background index ----------------------------------------------
    #
    # /api/index above indexes inside the request. That is fine for one small
    # collection and wrong for a first run: embedding a whole library runs at
    # roughly 50 chunks per second on a laptop CPU, so a large library holds
    # the connection open for minutes with nothing to show and the browser
    # eventually times out. That silence is what a first run feels like from
    # the outside, and it reads as a hang rather than as work in progress.
    #
    # These routes are additive. /api/index keeps its exact response shape,
    # so every existing caller is unaffected; the only behavior change there
    # is that it now refuses to start while a background run is active,
    # because two indexers sharing one embedding store corrupt it.

    @app.post("/api/index/start")
    def reindex_start(payload: dict = Body(default={})) -> dict:
        only = payload.get("collection")
        force = bool(payload.get("force", False))
        if only and only not in core.config.collection_names():
            raise HTTPException(404, f"Unknown collection {only!r}")
        with _index_lock:
            if _index_job["running"]:
                raise HTTPException(409, "An index run is already in progress.")
            _index_job.update(running=True, started=time.time(), lines=[],
                              done=False, error=None, report=None,
                              finished=None,
                              collection=only, force=force)
        threading.Thread(target=_run_index, args=([only] if only else None, force),
                         name="index-job", daemon=True).start()
        return _index_status()

    @app.get("/api/index/status")
    def reindex_status(brief: bool = Query(default=False)) -> dict:
        """brief=1 omits the progress lines[] (see _index_status)."""
        return _index_status(brief)

    # ---- conversations -------------------------------------------------

    @app.get("/api/conversations")
    def conversations(collection: str | None = Query(default=None)) -> list[dict]:
        conn = core.open_db()
        try:
            return convo.list_conversations(conn, collection)
        finally:
            conn.close()

    @app.post("/api/conversations")
    def create_conversation(payload: dict = Body(...)) -> dict:
        collection = payload.get("collection")
        if not collection:
            raise HTTPException(400, "collection is required")
        if collection != GLOBAL_COLLECTION and collection not in core.config.collection_names():
            raise HTTPException(404, f"Unknown collection '{collection}'")
        title = payload.get("title") or "New conversation"
        if not isinstance(title, str):
            raise HTTPException(400, "title must be a string")
        conn = core.open_db()
        try:
            return convo.create_conversation(conn, collection, title)
        finally:
            conn.close()

    @app.get("/api/conversations/{conversation_id}")
    def conversation_detail(conversation_id: int) -> dict:
        conn = core.open_db()
        try:
            try:
                c = convo.get_conversation(conn, conversation_id)
                msgs = convo.list_messages(conn, conversation_id)
            except BrainError as e:
                raise HTTPException(404, str(e))
            return {"conversation": c, "messages": msgs}
        finally:
            conn.close()

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: int) -> dict:
        conn = core.open_db()
        try:
            try:
                convo.delete_conversation(conn, conversation_id)
            except BrainError as e:
                raise HTTPException(404, str(e))
        finally:
            conn.close()
        return {"deleted": conversation_id}

    # ---- ask (SSE) -----------------------------------------------------

    def _ask_stream(question: str, collection: str, model: str | None,
                    conversation_id: int | None, images: list[dict] | None = None):
        """Shared SSE generator for conversation ask and stateless quick-ask."""
        has_images = bool(images)
        # What gets stored as the user turn: the typed text plus a marker so a
        # reloaded conversation shows an image was attached. The image bytes
        # themselves are per-turn and not persisted (history replays as text).
        stored_question = question
        if has_images:
            n = len(images)
            marker = f"[{n} image{'s' if n != 1 else ''} attached]"
            stored_question = f"{question}\n\n{marker}".strip()
        conn = core.open_db()
        try:
            history: list[dict] = []
            if conversation_id is not None:
                try:
                    history = convo.history_for_api(conn, conversation_id)
                except BrainError as e:
                    yield _sse("error", {"detail": str(e)})
                    return

            def record(assistant_text: str, *, prepared=None, model=None) -> None:
                """Persist the exchange. Nothing is written until we know how
                the turn resolved, so a failed prepare never leaves an
                orphaned user message that would duplicate on retry.

                A partial answer keeps its citations and model, so a reloaded
                conversation shows the same sources the live stream did."""
                if conversation_id is None:
                    return
                convo.add_message(conn, conversation_id, "user", stored_question)
                convo.add_message(
                    conn, conversation_id, "assistant", assistant_text,
                    citations=([c.as_dict() for c in prepared.citations]
                               if prepared is not None else None),
                    model=model,
                )

            try:
                prepared = core.prepare_ask(conn, question, collection,
                                            history=history, model=model,
                                            has_images=has_images)
            except AssistBlocked as e:
                detail = str(e)
                record(f"BLOCKED: {detail}")
                yield _sse("refusal", {
                    "reason": "assist_blocked", "detail": detail,
                    "collections": e.collections,
                })
                return
            except (NoRelevantResults, EmptyIndexError) as e:
                detail = str(e)
                record(str(e))
                yield _sse("refusal", {"reason": "no_results", "detail": detail,
                                       "collections": []})
                return
            except StoreOutOfSync as e:
                # Recoverable, and the app can recover it: an interrupted
                # index leaves chunks committed with no vectors, and the only
                # instruction we used to give was "run: brain index", a CLI
                # command a friend with no terminal cannot follow. Named as a
                # refusal reason with the collection attached so the caller
                # can offer a button that POSTs /api/index/start instead.
                detail = str(e)
                record(f"STORE OUT OF SYNC: {detail}")
                yield _sse("refusal", {
                    "reason": "store_out_of_sync", "detail": detail,
                    "collection": getattr(e, "collection", None),
                    "collections": [],
                })
                return
            except BrainError as e:
                yield _sse("error", {"detail": str(e)})
                return
            except Exception as e:
                yield _sse("error", {"detail": f"{type(e).__name__}: {e}"})
                return

            use_model = prepared.model or core.config.settings.default_model
            yield _sse("meta", {
                "model": use_model,
                "collection": collection,
                "effective_level": prepared.effective_level,
                "restricting_collections": prepared.restricting_collections,
                "empty_collections": prepared.empty_collections,
                "truncated": prepared.truncated,
                "dropped": prepared.dropped,
                "notices": prepared.notices(),
                "citations": [c.as_dict() for c in prepared.citations],
            })

            parts: list[str] = []
            try:
                for delta in core.stream_answer(prepared, model=model,
                                                history=history, images=images):
                    parts.append(delta)
                    yield _sse("delta", {"text": delta})
            except (MissingAPIKeyError, BrainError) as e:
                detail = str(e)
                if parts:  # keep a partial answer rather than losing it
                    record("".join(parts) + f"\n\n[interrupted: {detail}]",
                           prepared=prepared, model=use_model)
                yield _sse("error", {"detail": detail})
                return
            except Exception as e:  # API/network failures reported, not swallowed
                detail = f"{type(e).__name__}: {e}"
                if parts:
                    record("".join(parts) + f"\n\n[interrupted: {detail}]",
                           prepared=prepared, model=use_model)
                yield _sse("error", {"detail": detail})
                return

            answer = "".join(parts)
            if not answer.strip():
                # An empty completion is a failure, not an answer: persisting
                # it would leave a blank cited turn that looks successful.
                detail = ("The model returned an empty response. Nothing was saved. "
                          "Try again, or rephrase the question.")
                yield _sse("error", {"detail": detail})
                return

            message_id = None
            if conversation_id is not None:
                convo.add_message(conn, conversation_id, "user", stored_question)
                message_id = convo.add_message(
                    conn, conversation_id, "assistant", answer,
                    citations=[c.as_dict() for c in prepared.citations],
                    model=use_model,
                )
            yield _sse("done", {"message_id": message_id})
        finally:
            conn.close()

    # When an image is attached, the text is optional: a bare screenshot with
    # no question still has material to work from, so fall back to a default.
    IMAGE_DEFAULT_Q = "Help me with the attached image(s)."

    @app.post("/api/conversations/{conversation_id}/ask")
    def conversation_ask(conversation_id: int, payload: dict = Body(...)) -> StreamingResponse:
        images = _parse_images(payload)
        question = (payload.get("question") or "").strip()
        if not question and not images:
            raise HTTPException(400, "question is required")
        if not question:
            question = IMAGE_DEFAULT_Q
        conn = core.open_db()
        try:
            try:
                c = convo.get_conversation(conn, conversation_id)
            except BrainError as e:
                raise HTTPException(404, str(e))
        finally:
            conn.close()
        return StreamingResponse(
            _ask_stream(question, c["collection"], payload.get("model"),
                        conversation_id, images),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/ask")
    def quick_ask(payload: dict = Body(...)) -> StreamingResponse:
        """Stateless quick-ask. Always runs against 'all' (global-mode rules)."""
        images = _parse_images(payload)
        question = (payload.get("question") or "").strip()
        if not question and not images:
            raise HTTPException(400, "question is required")
        if not question:
            question = IMAGE_DEFAULT_Q
        return StreamingResponse(
            _ask_stream(question, GLOBAL_COLLECTION, payload.get("model"),
                        None, images),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- calendar ------------------------------------------------------

    @app.get("/api/events")
    def events(start: str = Query(...), end: str = Query(...)) -> list[dict]:
        try:
            s = datetime.fromisoformat(start)
            e = datetime.fromisoformat(end)
        except ValueError as ex:
            raise HTTPException(400, f"Bad date: {ex}")
        conn = core.open_db()
        try:
            rows = cal.events_between(conn, s, e)
        finally:
            conn.close()
        # Decorate with completion state here rather than in the calendar
        # module: completions are user state, not calendar data, and the
        # calendar table is rebuilt from scratch on every import.
        from .. import completions as comp

        is_done = comp.resolver(core.config)
        for r in rows:
            day = (r.get("starts_at") or "")[:10]
            r["done"] = is_done(r["course"], r["title"], day)
            r["done_key"] = day          # the client echoes course+title+date
        return rows

    @app.get("/api/weekload")
    def weekload() -> list[dict]:
        conn = core.open_db()
        try:
            return cal.week_load(conn, core.config)
        finally:
            conn.close()

    @app.get("/api/today")
    def today() -> dict:
        now = datetime.now()
        conn = core.open_db()
        try:
            next_up = cal.next_events(conn, now, limit=8, collapse_repeats=True)
            due7 = cal.due_within(conn, now, 7)
            weeks = cal.week_load(conn, core.config)
            stats = core.collection_stats(conn)
        finally:
            conn.close()
        this_monday = cal.monday_of(date.today()).isoformat()
        upcoming = [w for w in weeks if w["week_start"] >= this_monday]
        worst = max(upcoming, key=lambda w: w["count"]) if upcoming else None
        return {
            "due_next_7_days": due7,
            "worst_week": worst,
            "collections_indexed": sum(1 for s in stats if s["chunk_count"] > 0),
            "collections_total": len(stats),
            "next_up": next_up,
            "weekload": weeks,
            "current_week": this_monday,
        }

    @app.get("/api/analytics")
    def analytics_endpoint() -> dict:
        conn = core.open_db()
        try:
            return analytics.build(conn, core.config)
        finally:
            conn.close()

    @app.get("/api/weather")
    def weather_endpoint(force: bool = Query(default=False)) -> dict:
        u = core.config.user
        if not u.has_location:
            return {
                "ok": False,
                "error": "No location configured. Set [user].latitude and "
                         "[user].longitude in config.toml.",
                "location": "",
            }
        return weather.get_weather(u.latitude, u.longitude, u.location_label,
                                   force=force)

    @app.get("/api/socials")
    def socials_list(day: str | None = Query(None)) -> dict:
        """Happy hours, kept OUT of the deadline calendar on purpose.

        week_load selects coursework by EXCLUSION (`WHERE kind != 'admin'`),
        so a social event added to the events table would silently inflate
        the workload chart and the due counts. Socials have their own store
        and their own tab.
        """
        from datetime import date as _date

        from .. import socials as soc

        when = None
        if day:
            try:
                when = _date.fromisoformat(day[:10])
            except ValueError:
                raise HTTPException(400, "day must be YYYY-MM-DD")
        when = when or _date.today()
        # LOCAL ONLY, so the tab paints instantly. The three campus feeds
        # live behind /api/socials/events and are fetched after first paint -
        # waiting on someone else's server before showing a file we already
        # have on disk is what made this tab feel broken.
        venues = soc.load(core.config)
        return {
            "day": when.isoformat(),
            "weekday": when.weekday(),
            "today": [h.to_dict() for h in soc.today(core.config, when)],
            "all": [h.to_dict() for h in venues],
            "confirmed": sum(1 for h in venues if h.confirmed),
            "total": len(venues),
        }

    @app.get("/api/socials/events")
    def socials_events() -> dict:
        """Campus, athletics and student-org events. Network-touching, and
        memo-cached so revisiting the tab is instant."""
        from .. import socials as soc

        now = time.time()
        cached = getattr(app.state, "_socials_cache", None)
        if cached and now - cached[0] < 900:          # 15 minutes
            events, errs = cached[1], cached[2]
        else:
            try:
                evs, errs = soc.fetch_events(core.config, days=14)
                events = [e.to_dict() for e in evs]
            except Exception as e:
                events, errs = [], [f"{type(e).__name__}: {e}"]
            app.state._socials_cache = (now, events, errs)
        return {"events": events, "feed_errors": errs}

    @app.get("/api/update")
    def update_status() -> dict:
        """Is a newer build available, or already downloaded and waiting?

        Cheap and network-free unless a check is asked for: the frontend
        polls this, and an app that stalls its own UI on someone else's
        server is worse than one that is a day out of date.
        """
        from .. import updates

        waiting = updates.pending(core.config)
        return {
            "current": updates.current_version(),
            "configured": bool((core.config.settings.update_url or "").strip()),
            "pending": waiting,
        }

    @app.post("/api/update/check")
    def update_check() -> dict:
        from .. import updates

        avail = updates.check(core.config)
        return {"current": updates.current_version(),
                "available": avail.to_dict() if avail else None}

    @app.post("/api/update/download")
    def update_download() -> dict:
        """Download + verify the new build and park it for the next launch.
        Never installs into the running process - see brain/updates.py."""
        from .. import updates

        res = updates.stage(core.config)
        if not res.get("staged"):
            raise HTTPException(400, res.get("reason", "could not download"))
        return res

    @app.post("/api/complete")
    def set_complete(payload: dict = Body(...)) -> dict:
        """Mark one obligation finished, or put it back. Identity travels as
        (course, title, date) so the client never depends on an event id -
        those change whenever a deadline is retimed."""
        from .. import completions as comp

        course = str(payload.get("course") or "").strip()
        title = str(payload.get("title") or "").strip()
        date = str(payload.get("date") or "").strip()[:10]
        if not course or not title or len(date) != 10:
            raise HTTPException(400, "course, title and date are required")
        rec = comp.set_done(core.config, course=course, title=title,
                            date=date, done=bool(payload.get("done", True)))
        return {"ok": True, "course": rec.course, "date": rec.date,
                "done": rec.done}

    @app.post("/api/calendar/reimport")
    def calendar_reimport() -> dict:
        try:
            report = core.calendar_import()
        except BrainError as e:
            raise HTTPException(500, str(e))
        return core.calendar_status() or {"total_imported": report.total_imported}

    # ---- assignment sync (dry-run poller; applying stays explicit) -----

    @app.get("/api/sync/status")
    def sync_status() -> dict:
        """Latest background-poll findings: per-site new/moved counts and the
        list of new/moved items. Cached; does not hit the network."""
        return app.state.sync_poller.status.to_dict()

    @app.post("/api/sync/run")
    def sync_run_now() -> dict:
        """Trigger an immediate dry-run poll and return the fresh status.

        Blocks for the whole scrape. Measured live against four sites: 14.2
        seconds, and the connectors allow 25 seconds per request, so a slow
        campus server can push this well past a minute. Prefer
        /api/sync/start for anything a person is waiting on."""
        return app.state.sync_poller.poll_once().to_dict()

    @app.post("/api/sync/start")
    def sync_start() -> dict:
        """Kick off the same dry-run poll and return at once.

        The caller then polls /api/sync/status, whose `running` flag the
        poller already maintains, so a button can show real progress instead
        of sitting disabled on "Checking..." for a quarter of a minute with
        no way to tell a slow check from a hung one.

        Safe to call twice: poll_once() serializes on the poller's own lock,
        so a second caller waits for the in-flight scrape rather than
        starting a competing one. We skip spawning that waiter entirely.
        """
        poller = app.state.sync_poller
        if poller.status.running:
            return poller.status.to_dict()
        threading.Thread(target=poller.poll_once, name="sync-now",
                         daemon=True).start()
        return poller.status.to_dict()

    @app.post("/api/sync/apply")
    def sync_apply() -> dict:
        """Apply currently-found new/moved items to the calendar. Explicit:
        this is the only path that writes, and only when the user asks."""
        from .. import sync as syncmod

        conn = core.open_db()
        try:
            report = syncmod.run(core.config, conn, apply=True)
        except BrainError as e:
            raise HTTPException(500, str(e))
        finally:
            conn.close()
        if report.applied:
            core.calendar_import()
        # Name what was written. "Calendar updated" alone is unfalsifiable:
        # an applied item is often an admin row dated weeks out, which the
        # Today plan does not render at all (it shows exam/project/quiz), so
        # a successful apply can look identical to a broken one.
        items = []
        for s in report.sites:
            if not s.recon:
                continue
            for c in list(s.recon.new) + list(s.recon.moved):
                items.append({"course": c.item.course, "title": c.item.title,
                              "date": c.item.date, "kind": c.item.kind,
                              "change": c.kind})
        # Refresh the cached status OFF the request. syncmod.run(apply=True)
        # above already scraped every site; a second poll_once() here made
        # applying cost two full network passes (measured ~6s + ~14s), which
        # is why the button sat dead for 20-30 seconds. The authoritative
        # refresh still happens, the caller just does not wait for it - it
        # watches status.running the same way the re-check button does.
        poller = app.state.sync_poller
        if not poller.status.running:
            threading.Thread(target=poller.poll_once, name="sync-after-apply",
                             daemon=True).start()
        return {"applied": report.applied, "items": items,
                "status": poller.status.to_dict()}

    @app.post("/api/sync/news/apply")
    def sync_news_apply() -> dict:
        """Mark the current announcements as read, and file them.

        Deliberately NOT folded into /api/sync/apply. That button says
        "Apply to calendar" and writes deadlines; announcements are a
        different thing and quietly consuming them there would mean a user
        who wanted a deadline written also silently lost their unread mail.

        This existed only as `brain sync news --apply` on the CLI, so from
        the app there was NO way to clear an announcement: check_news is
        called with apply=False by the poller, /api/sync/apply never touches
        news, and the count therefore never returned to zero. Because that
        count feeds the sync total, a single unread announcement also
        permanently suppressed the "No new deadlines" line.

        apply=True also writes each one as Markdown under
        <course>/_synced/announcements/, so Chat can cite it - a feature that
        was unreachable from the UI for the same reason.
        """
        from .. import sync as syncmod

        try:
            report = syncmod.check_news(core.config, apply=True)
        except BrainError as e:
            raise HTTPException(500, str(e))
        touched = sorted({n["course"] for n in report.new})
        # Index the new files in the BACKGROUND. Embedding a course takes
        # long enough to time out a browser, and the announcements are
        # already filed and marked seen by this point, so nothing is lost if
        # the caller walks away.
        started = False
        if touched:
            with _index_lock:
                if not _index_job["running"]:
                    _index_job.update(running=True, started=time.time(),
                                      lines=[], done=False, error=None,
                                      report=None, collection=", ".join(touched),
                                      force=False)
                    started = True
            if started:
                threading.Thread(target=_run_index, args=(touched, False),
                                 name="index-news", daemon=True).start()
        return {
            "saved": report.saved,
            "courses": touched,
            "indexing": started,
            "errors": [{"site": s, "message": m} for s, m in report.errors],
            "status": app.state.sync_poller.status.to_dict(),
        }

    # ---- session push (browser extension keeps cookies fresh) ----------

    # Map the domains the extension watches to our connector names. A pushed
    # cookie set for any of these refreshes that site's session, so the daily
    # OAKS re-paste disappears as long as the user stays logged in.
    SESSION_DOMAINS = {
        "lms.cofc.edu": "oaks",
        "vhlcentral.com": "vhl",
        "m3a.vhlcentral.com": "vhl",
        "newconnect.mheducation.com": "connect",
        "mheducation.com": "connect",
        "library.blended-teaching.com": "blended",
    }

    @app.post("/api/session/push")
    def session_push(request: Request, payload: dict = Body(...)) -> dict:
        """Receive a fresh cookie set from the local browser extension and
        store it. Gated to the extension by a custom header the browser will
        not attach cross-origin without a CORS preflight we never grant, plus
        the existing localhost-only Host guard."""
        if request.headers.get("x-cc-extension") != "1":
            raise HTTPException(403, "extension header required")
        site = payload.get("site")
        if not site:
            domain = (payload.get("domain") or "").lower().lstrip(".")
            site = SESSION_DOMAINS.get(domain)
        # oaks/vhl/connect/blended are sync connectors; google/sharepoint are
        # auxiliary sessions the link resolver reads.
        if site not in {"oaks", "vhl", "connect", "blended", "google", "sharepoint"}:
            raise HTTPException(400, f"unknown site for push: {site!r}")
        cookies = payload.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            raise HTTPException(400, "cookies must be a non-empty object")
        cookies = {str(k): str(v) for k, v in cookies.items()}

        from ..connectors import SessionStore

        store = SessionStore(core.config.settings.data_dir)
        # Preserve a previously-captured base_url (VHL's section URL, etc.)
        # unless the push supplies a new one.
        base_url = payload.get("base_url") or ""
        if not base_url and store.has(site):
            try:
                base_url = store.load(site).get("base_url", "")
            except Exception:
                base_url = ""
        store.save(site, cookies, base_url=base_url)
        # A fresh OAKS session is exactly what the grades cache needs; refresh
        # it now so the panel fills the moment the extension reconnects,
        # without the user hitting the tab's own refresh. Best-effort.
        if site == "oaks":
            try:
                from .. import grades as grades_mod

                grades_mod.refresh(core.config)
            except Exception:
                pass
        return {"ok": True, "site": site, "cookies": len(cookies)}

    # ---- browser-fetched link documents --------------------------------
    # Google Docs and SharePoint files are behind org auth that blocks
    # server-side fetching. The extension, already logged in, fetches them in
    # the browser and posts the bytes back here.

    @app.get("/api/links/pending")
    def links_pending() -> dict:
        from .. import sync as syncmod

        items = syncmod.load_links_pending(core.config)
        # Don't hand the extension our filesystem path.
        safe = [{k: v for k, v in it.items() if k != "dest"} for it in items]
        return {"pending": safe}

    MAX_LINK_BYTES = 40 * 1024 * 1024

    @app.post("/api/links/content")
    def links_content(request: Request, payload: dict = Body(...)) -> dict:
        """Save one browser-fetched document, matched by link id."""
        if request.headers.get("x-cc-extension") != "1":
            raise HTTPException(403, "extension header required")
        import base64 as _b64

        from .. import sync as syncmod

        lid = payload.get("id")
        data_b64 = payload.get("content")
        if not lid or not isinstance(data_b64, str) or not data_b64:
            raise HTTPException(400, "id and base64 content are required")
        if len(data_b64) > MAX_LINK_BYTES:
            raise HTTPException(413, "document too large")
        try:
            content = _b64.b64decode(data_b64)
        except Exception:
            raise HTTPException(400, "content is not valid base64")
        try:
            result = syncmod.save_browser_fetched(core.config, lid, content)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return result

    @app.post("/api/links/imported")
    def links_imported(request: Request, payload: dict = Body(...)) -> dict:
        """Import a doc the extension downloaded via chrome.downloads into
        Downloads/cc-links/<id>.<ext> (the CORS-free path for Google docs)."""
        if request.headers.get("x-cc-extension") != "1":
            raise HTTPException(403, "extension header required")
        from .. import sync as syncmod

        lid = payload.get("id")
        if not lid:
            raise HTTPException(400, "id is required")
        try:
            result = syncmod.import_downloaded_link(core.config, lid)
        except FileNotFoundError:
            raise HTTPException(404, "downloaded file not found yet")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if result.get("course"):
            try:
                core.index(only=[result["course"]])
            except BrainError:
                pass
        return result

    @app.post("/api/links/diag")
    def links_diag(request: Request, payload: dict = Body(...)) -> dict:
        """Per-doc fetch outcomes from the extension, for diagnosing failures."""
        if request.headers.get("x-cc-extension") != "1":
            raise HTTPException(403, "extension header required")
        p = Path(core.config.settings.data_dir) / "links_diag.json"
        try:
            p.write_text(json.dumps(payload.get("diag", []), indent=1), encoding="utf-8")
        except OSError:
            pass
        return {"ok": True}

    @app.post("/api/links/reindex")
    def links_reindex(request: Request, payload: dict = Body(default={})) -> dict:
        """Reindex the collections whose link docs just arrived (extension
        calls this once after a fetch batch)."""
        if request.headers.get("x-cc-extension") != "1":
            raise HTTPException(403, "extension header required")
        courses = payload.get("courses") or core.config.collection_names()
        courses = [c for c in courses if c in core.config.collection_names()]
        if courses:
            try:
                core.index(only=courses)
            except BrainError as e:
                raise HTTPException(500, str(e))
        return {"reindexed": courses}

    # ---- grades --------------------------------------------------------

    @app.get("/api/grades")
    def grades_endpoint() -> dict:
        """Cached gradebooks only - this GET never hits the network. A live
        scrape belongs behind an explicit POST: an inline first-call fetch
        could pin a page load for minutes (25s httpx timeout per request, no
        overall budget) and made a credentialed scrape reachable from a bare
        GET. A cold cache returns needs_refresh so the frontend can offer
        the fetch instead."""
        from .. import grades as grades_mod

        data = grades_mod.load_cached(core.config)
        if not data.get("fetched_at"):
            data["needs_refresh"] = True
        # Derived here, not in the browser, so one implementation owns the
        # scale and the tests cover it. Courses with nothing graded are
        # excluded rather than counted as zero.
        data["gpa"] = grades_mod.gpa_summary(data.get("courses") or [])
        return data

    @app.post("/api/grades/refresh")
    def grades_refresh_endpoint() -> dict:
        """Fetch live from the gradebook sites right now and re-cache."""
        from .. import grades as grades_mod

        return grades_mod.refresh(core.config)

    # ---- daily plan ----------------------------------------------------

    @app.get("/api/plan")
    def plan_endpoint() -> dict:
        """Today's (and tomorrow-morning's) actionable items, in order.
        kind=admin rows are included but flagged; class meetings excluded."""
        import re as _re

        now = datetime.now()
        end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        start_day = now.replace(hour=0, minute=0, second=0)
        conn = core.open_db()
        try:
            rows = conn.execute(
                "SELECT course, title, starts_at, all_day, kind FROM events "
                "WHERE kind != 'recurring' AND starts_at >= ? AND starts_at <= ? "
                "ORDER BY starts_at",
                (start_day.isoformat(timespec="seconds"),
                 end.isoformat(timespec="seconds")),
            ).fetchall()
        finally:
            conn.close()
        items = []
        today_iso = now.date().isoformat()
        for r in rows:
            starts = r["starts_at"]
            all_day = bool(r["all_day"])
            est = None
            m = _re.search(r"est\s+((?:\d+h\s*)?\d+m)|\((\d+m)\)", r["title"])
            if m:
                est = m.group(1) or m.group(2)
            # An all-day deadline is stored at T00:00:00 but is due all day
            # (calendar.py's 23:59:59 cutoff convention): it only becomes
            # "past" once its date is over, never at midnight of its due day.
            past = (starts[:10] < today_iso) if all_day \
                else starts < now.isoformat(timespec="seconds")
            items.append({
                "course": r["course"],
                "title": r["title"],
                "starts_at": starts,
                "all_day": all_day,
                "day": "today" if starts[:10] == today_iso else "tomorrow",
                "past": past,
                "kind": r["kind"],
                "graded": cal.looks_graded(r["title"], r["kind"]),
                "estimated": est,
            })
        return {"now": now.isoformat(timespec="seconds"), "items": items}

    # ---- static frontend (mounted last; catches everything else) -------

    class _RevalidatingStatic(StaticFiles):
        """Serve the app's own HTML/JS/CSS with `Cache-Control: no-cache`.

        StaticFiles sends an ETag and Last-Modified but no Cache-Control, so
        browsers fall back to HEURISTIC caching and will happily serve a
        stale app.js without asking. That is not cosmetic: it hid a whole new
        tab on a reused browser profile, and it would break every self-update
        by pairing new backend code with the previous frontend.

        `no-cache` does NOT mean "do not cache" - it means "revalidate before
        use", so the ETag still yields a cheap 304 on every load. Fonts and
        vendored libraries are content-stable and keep a long cache.
        """

        _LONG = (".woff", ".woff2", ".ttf", ".otf", ".ico", ".png", ".jpg", ".svg")

        def file_response(self, full_path, stat_result, scope, status_code=200):
            resp = super().file_response(full_path, stat_result, scope,
                                         status_code=status_code)
            path = str(full_path).lower()
            if path.endswith(self._LONG) or "/vendor/" in path.replace("\\", "/"):
                resp.headers["Cache-Control"] = "public, max-age=604800"
            else:
                resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/", _RevalidatingStatic(directory=STATIC_DIR, html=True),
              name="static")
    return app
