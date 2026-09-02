# Command Center

Local personal knowledge app: course files, REUW materials, and the Obsidian
vault indexed into scoped collections; cited chat against them; semester
calendar and workload. Single user, localhost only. Spec: [SPEC.md](SPEC.md).

## Setup

```
uv sync                      # Python 3.12 venv + all deps
uv run brain index           # first run downloads BAAI/bge-small-en-v1.5 (~130 MB)
uv run brain calendar import
uv run brain serve           # http://127.0.0.1:8177
```

### Who pays for the answers

`[settings] backend` in `config.toml` picks the meter. **No API key is needed
by default.**

| backend | Credential | Billing |
|---|---|---|
| `subscription` (default) | your existing Claude Code login | draws on the Claude Pro/Max plan you already pay for |
| `api` | `ANTHROPIC_API_KEY` | Anthropic API, pay-as-you-go, billed separately |

This distinction is easy to get wrong: **a Claude Pro/Max subscription does not
include API credit.** They are separate products on separate meters, so the
`api` backend is a second bill on top of the subscription. The `subscription`
backend exists so this app runs on the plan you already have.

Retrieval, citations, the similarity floor, and the `assist_level` gate are
identical on both — the backend only decides who bills the request.

**Subscription backend** needs the `claude` CLI on PATH and a completed
`claude` login. Nothing else. It runs with every tool disabled and
`max_turns = 1`, so the model answers only from the excerpts this app
retrieved — it never reads your disk, searches the web, or inherits your
`CLAUDE.md`.

**API backend**: set `backend = "api"`, then copy `.env.example` to `.env` and
put the key in it:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored and read at startup; a real environment variable beats it.
Get a key at [Settings → API keys](https://platform.claude.com/settings/keys).
Optionally cap spend with `[settings] max_budget_usd`.

The top bar warns only about what the *configured* backend actually needs.

**The app is the web UI**: run `uv run brain serve` and open
<http://127.0.0.1:8177>. Five tabs, each linkable by hash
(`#today`, `#analytics`, `#calendar`, `#chat`, `#library`):

| Tab | What it carries |
|---|---|
| **Today** | greeting, local weather, six stat tiles, next 8 deadlines with countdowns, deadline density for the next 28 days |
| **Analytics** | semester workload stacked by course, remaining work per course (chart + table), index composition, chunks by file type, heaviest days ahead |
| **Calendar** | month grid / week list, week-load heat row, event popover with "discuss" |
| **Chat** | per-collection conversations, streaming cited answers, inline diagrams |
| **Library** | per-collection stats, parse failures, calendar sources, reindex |

All five tabs work with no API key on the default `subscription` backend.

### Charts

Colors are the course hues from `config.toml`, used identically in every tab.
They were re-picked against the dark surface and **validated** for
colorblind separation, lightness band, chroma, and contrast - the previous
set put FINC389's green and FINC380's orange 3.1 ΔE apart under protanopia,
which is indistinguishable. Every chart pairs color with a second channel (a
legend, a labeled table, or direct value labels), so identity is never
carried by hue alone.

## Indexed file types

| Type | Chunked by | Locator |
|---|---|---|
| `.md` | heading | heading path (`Intro > Setup`) |
| `.pdf` | page | `page 12` |
| `.pptx` | slide, incl. speaker notes, tables, grouped shapes | `slide 7` |
| `.docx` | Heading 1 / Heading 2, incl. nested tables | heading path |
| `.xlsx` | worksheet, **formulas kept as written** | `sheet Loan` |
| `.html` | whole document, script/style dropped | `<title>` |
| `.txt` | packed text | `part 3` |

Image-only files (scans with no text layer) are reported as failures in the
Library tab rather than silently counted as indexed. OCR is not included.

## CLI

```
brain index [--collection X] [--force]
brain search "q" [--collection X|all] [-k N]
brain ask "q" --collection X|all [-k N] [--model ...]
brain collections
brain calendar import
brain calendar next [--days N]
brain serve [--port 8177]
```

All commands take `--config path\to\config.toml`; by default the config is
found by walking up from the current directory.

## Layout

```
src/brain/            core library - ALL logic lives here
  config.py           config.toml loading + validation
  db.py               SQLite schema (chunks, conversations, messages, events)
  chunking.py         md/pdf/pptx/docx/txt -> chunks with locators
  embeddings.py       bge-small embedder + .npy matrix store
  indexer.py          incremental indexing (mtime+size), store sync
  retrieval.py        brute-force cosine; global mode = top-k PER collection
  gate.py             assist_level enforcement (hard branch, pre-API)
  ask.py              prepare (retrieve/gate/budget) + stream (Anthropic)
  calendar.py         ICS/CSV/recurring import, week-load
  conversations.py    chat persistence
  core.py             facade used by both frontends
  cli.py              typer CLI (thin)
  web/app.py          FastAPI + SSE (thin)
  web/static/         index.html, app.js, style.css (vanilla, no build)
config.toml           collections, assist levels, colors, calendar
calendar/fixed.csv    dated deadlines (source: _Fall2026_Reference.md)
data/                 brain.db, embeddings.npy, embeddings_ids.json
```

## fixed.csv schema

```
course,title,date,start_time,end_time,all_day,kind
FINC313,Chapter 8 Quiz,2026-09-26,12:00,,false,quiz
```

`date` is YYYY-MM-DD, times HH:MM 24h (both optional; empty = all-day),
`kind` is one of `exam|project|quiz|admin`. Bad rows are reported with line
numbers and skipped; a bad header fails the whole file and keeps the
previously imported CSV events.

## Behavior notes

- **assist_level** is enforced in code before any API call. `off` refuses
  before retrieval even runs; in global mode ("all") the effective level is
  the most restrictive among collections that returned chunks, and any `off`
  collection with hits blocks the whole request by name. When `explain_only`
  wins, the answer says which collection caused it.
- **Similarity floor**: if nothing scores at or above it, the app says
  "nothing relevant indexed" and does not call the API. The code default is
  0.3, but `config.toml` sets **0.60**, calibrated against this index -
  bge-small compresses cosine similarity into a high narrow band, so 0.3
  never rejects anything. Measured on real content: topical questions score
  0.64-0.77, off-topic ones 0.46-0.58. **Re-measure if you change
  `embedding_model`.**
- **Global mode budget** is spent round-robin across collections, so a large
  high-scoring collection cannot crowd the others out of the context window
  the way pure score ordering would.
- **Follow-up questions** ("explain that more simply") are anchored with the
  previous user turns before embedding, so short follow-ups do not fall under
  the similarity floor mid-conversation.
- **Chunk sizing follows the model, not a fixed number.** The spec says
  ~800 tokens, but bge-small only reads 512 - and a chars/4 estimate
  underestimates real tokens by ~1.45x (5x worst case), so estimate-sized
  chunks overflowed and the tail was dropped before it was ever embedded
  (measured: **20.6% of all indexed text**). The indexer now packs with the
  model's own tokenizer, up to its real window. `chunking.py` still defaults
  to the spec's 800/100 for model-agnostic use.
- **Store integrity.** `chunks.id` is a plain SQLite rowid, so deleting a
  file's chunks frees ids the next insert reuses. If an index run is killed
  between the DB commit and the embedding save (minutes, on a full run), a
  reused id could end up paired with the vector of the text it replaced -
  returning confident answers citing the wrong file. Every vector is stored
  with its chunk's content hash; a mismatch makes search fail loud, and an
  ordinary `brain index` re-embeds exactly the affected rows.
- **Untrusted content**: indexed documents and imported ICS files are treated
  as data, never instructions - the system prompt says so explicitly and the
  integrity rules are stated after the excerpts they govern. The web app
  sends a strict CSP (only the marked/DOMPurify CDN may load scripts; no
  off-origin images, fetches, or form posts), so a prompt injection in an
  indexed file cannot beacon retrieved context out through markup. Downloads
  from a diagram/widget block hand over the sanitized markup, not the raw
  model output.
- **Deadline vs. meeting kinds**: Today's "due next 7 days" metric and
  "Next up" list count only exam/quiz/project. The week-load strip and heat
  row count everything except `admin` (per spec), so class meetings are
  included there.
- **Subscribed feeds keep the calendar current.** `[calendar] ics_urls` holds
  iCal subscription URLs, refetched on every import - so a deadline added or
  moved in OAKS appears on the next `brain calendar import` without exporting
  anything by hand. Get the OAKS link from Calendar -> Settings -> "Enable
  Calendar Feeds" -> Subscribe -> "All Calendars and Tasks". A feed that
  fails to fetch falls back to its last downloaded copy and says so, and a
  response that isn't an iCalendar document (a login page returned as HTTP
  200) is refused rather than imported as an empty calendar.
- **Calendar import** is idempotent (content-hash event ids). A source is
  DELETEd and rebuilt only when every one of its inputs parsed with zero
  errors; if anything failed, its events are updated in place instead, so a
  broken input can never wipe your calendar. (Opening `fixed.csv` in Excel
  and saving reformats every date - that used to be enough to delete all 56
  deadlines.) When that happens the import says so and items removed at the
  source may linger until you fix the errors and reimport.
- **ICS recurrence** (`RRULE`/`RDATE`/`EXDATE`) is expanded across the
  semester window, and `DURATION` is honored when `DTEND` is absent. All-day
  `DTEND` is treated as exclusive per RFC 5545.
- **All-day deadlines** stay "upcoming" until the end of their day, so
  something due today does not vanish from Today at 00:01.
- **A file that yields no text is a failure, not a success.** Recording it as
  indexed would hide it forever behind the mtime/size skip, so it is listed
  in the Library tab with a reason instead.
- **An unreadable collection root is never read as "everything was deleted."**
  If a drive is unplugged or a cloud folder is mid-sync, that collection's
  existing entries are kept and the problem is reported.

## Known environment caveats

- Windows Smart App Control / Application Control blocked
  `scipy...\_rotation_cy...pyd` once during the very first model load (an
  sklearn import path inside sentence-transformers). Retrying after the model
  download completed worked. If it recurs, the fallback is to swap
  `BgeEmbedder` to raw `transformers` + CLS pooling, which avoids
  sklearn/scipy entirely.
- Fall break is confirmed only for Mon 10/12. If it also covers Tue 10/13,
  the Tu/Th recurring meetings that day are phantoms - remove by adding
  10/13 to `[[calendar.breaks]]` and reimporting.
