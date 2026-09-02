# Command Center — Build Spec (v3, consolidated)

This supersedes the earlier CLI-only and web-extension prompts. One build.

## What this is

A local personal knowledge app: index my course files, REUW materials, and
Obsidian vault into scoped collections; chat against them with citations;
show my semester calendar and workload. Single user, localhost only.

## Principles (non-negotiable)

1. Core logic (index, retrieve, ask, calendar) lives in a library module.
   The CLI and the web app are both thin callers. No retrieval logic in
   route handlers.
2. Fail loud. Parse failures, missing paths, empty indexes, failed feed
   fetches — all print/report explicitly. Never return zero results as if
   that were an answer.
3. Collections are scoped. Cross-collection behavior is opt-in and
   governed by the rules in GLOBAL MODE below.
4. assist_level is enforced in code before any API call, never only in
   the prompt.

## Stack

- Python 3.12, uv
- typer + rich (CLI), FastAPI + uvicorn + SSE (web), localhost only
- sqlite3 stdlib; embeddings as a numpy .npy matrix on disk
- sentence-transformers, BAAI/bge-small-en-v1.5, local CPU
- pymupdf, python-docx, python-pptx, plain read for md/txt
- icalendar for ICS import
- anthropic SDK; ANTHROPIC_API_KEY from env, never in code
- Frontend: single static page, vanilla JS, no build step; marked +
  DOMPurify from CDN
- Do NOT add: vector DB, LangChain, React, ORM

## Models

- Default: claude-sonnet-4-6
- Per-message model picker in the UI with claude-fable-5 as the optional
  upgrade; show which model answered on each message
- Cap retrieval context at a configurable token budget (default ~8k);
  truncate lowest-scoring chunks first and say in the UI when truncation
  happened

## Config (config.toml)

[[collection]]
name, root paths, include/exclude globs,
assist_level = "full" | "explain_only" | "off",
color (hex, used consistently across all tabs)

Collections (all assist_level = full): FINC313, FINC315, FINC380, FINC389,
SPAN200, REUW, obsidian.

[calendar]
ics_paths (list), fixed_csv path, plus recurring rules:
{course, title, weekdays, time} and a breaks list of no-class dates.

## Data model (one SQLite file)

chunks(id, collection, source_path, locator, text, content_hash)
embeddings: aligned .npy matrix + id order manifest
conversations(id, collection, title, created_at, updated_at)
messages(id, conversation_id, role, content, citations_json, model,
         created_at)
events(id, course, title, starts_at, ends_at, all_day, kind, source)
  kind: exam | project | quiz | recurring | admin
  source: ics | csv | recurring

## Indexing

- Skip unchanged files (mtime + size); --force reindexes
- Chunking: md by heading (keep heading path); PDF per page (keep page
  number); PPTX per slide incl. speaker notes; DOCX by Heading 1/2
- ~800 tokens per chunk, 100 overlap, never split mid-sentence
- rich progress bar; per-collection counts; failed files listed at the end

## Retrieval

- Cosine over the matrix, brute force
- Similarity floor (default 0.3, configurable): below it, say "nothing
  relevant indexed" and do not call the API
- Answers cite source path + locator per claim

## assist_level gate (optional, defaults to full)

The gate is dormant when a collection is "full" (the default for all of them):
answers are unrestricted. The other two levels remain available if ever wanted:
- full: normal, unrestricted (current setting everywhere)
- explain_only: system instruction — may explain concepts and quiz the
  user; must refuse to draft, solve, translate for submission, or check
  answers on anything graded
- off: refuse, name the collection and the reason
The gate is a hard branch, not a prompt suggestion.

## GLOBAL MODE ("all")

- Retrieve top-k per collection separately (no merged pool), so one big
  collection can't crowd out the rest
- Every chunk tagged with its collection; citations must name it
- Effective assist_level = MOST RESTRICTIVE among collections that
  returned chunks. Any "off" → refuse and name the blocker. Any
  "explain_only" → whole response is explain_only.
- Enforced in code before the API call

## Calendar subsystem

- Import: parse each ics_path and fixed_csv into events; expand recurring
  rules across the semester window, skipping breaks; content-hash IDs so
  re-import updates rather than duplicates
- `reimport` available from CLI and from the Library tab
- Week-load computation: count of events (excluding kind=admin) per
  Monday-started week — this feeds the Today tab and Calendar heat row

## Web UI — four tabs

Shared: top bar with app name, model picker, active tab. Course colors
from config used identically everywhere.

TODAY
- Metric cards: due next 7 days; worst upcoming week (+count);
  collections indexed
- "Next up" list: next 8 events, color dot, course, title, due time
- Mini week-load strip for the remaining semester (bar per week, current
  week marked)
- Quick-ask box: runs against "all" with global-mode rules; if refused or
  restricted, show which collection caused it inline

CALENDAR
- Month grid and week list toggle; events colored by course; all-day and
  timed both supported
- Click an event → detail popover (title, course, kind, source) with a
  "discuss" button that opens a Chat conversation pre-scoped to that
  course's collection
- A thin week-load heat row above the month grid

CHAT
- Left rail: collections (with color + assist_level badge), then
  conversations within the selected collection
- Conversation belongs to exactly one collection (or "all"); cannot move
- Streaming responses; citations rendered as chips (path + locator)
- Visual output: system prompt instructs the model to emit diagrams as
  ```svg fences and widgets as ```html fences when content is spatial,
  sequential, or comparative — not decoratively. Frontend renders those
  fences inline. MANDATORY: DOMPurify with SVG/HTML profiles, strip
  script tags and on* attributes, before DOM insertion. Per-block
  view-source toggle and download button.

LIBRARY
- Per collection: doc count, chunk count, last indexed, assist_level
  badge, root paths, reindex button
- Parse failures listed with paths; calendar reimport button; feed/csv
  source status

## CLI (kept working)

brain index [--collection X] [--force]
brain search "q" [--collection X] [-k N]
brain ask "q" --collection X [-k N] [--model ...]
brain collections
brain calendar import | brain calendar next [--days N]
brain serve  (starts the web app)

## Build order

M1: core module + config + indexing + search + assist gate + CLI
M2: ask with streaming + citations, CLI first
M3: FastAPI + Chat tab (conversations, streaming, fence rendering)
M4: calendar import + Calendar and Today tabs
M5: Library tab + polish
Each milestone runnable and tested before the next.

## Tests (pytest)

- Chunking per format (heading paths, page/slide locators, overlap)
- assist_level gate: all three levels, plus global-mode most-restrictive
  resolution including the "off blocks all" case
- Similarity floor refusal
- Calendar: recurring expansion skips breaks; re-import idempotent
- Global retrieval: per-collection top-k, no pool merging

## Scope (updated 2026-08-24)

Single user, localhost only, no cloud sync, no mobile.

Calendar data arrives from: exported ICS/CSV, subscribed iCal feeds, AND
credentialed sync of the user's OWN accounts (OAKS/D2L, McGraw Hill Connect,
VHL Supersite, Blended Teaching). Sync reuses the user's existing browser
session (cookies) against each platform's own JSON/API endpoints - the user
is the account holder pulling their own assignment data. No password storage,
no third-party accounts, no evasion of a login the user hasn't completed.

No academic-integrity restriction on the assistant. assist_level defaults to
"full" for every collection; "explain_only"/"off" remain available as optional
per-collection opt-ins but are not used.
