"""brain - the Command Center CLI. Thin caller over brain.core; no logic here.

Commands:
  brain index [--collection X] [--force]
  brain search "q" [--collection X] [-k N]
  brain ask "q" --collection X [-k N] [--model ...]
  brain collections
  brain calendar import | brain calendar next [--days N]
  brain serve
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .ask import GLOBAL_COLLECTION
from .core import Core
from .errors import AssistBlocked, BrainError, EmptyIndexError, NoRelevantResults

app = typer.Typer(help="Command Center - scoped collections, cited chat, semester calendar.", no_args_is_help=True)
calendar_app = typer.Typer(help="Calendar import and queries.", no_args_is_help=True)
app.add_typer(calendar_app, name="calendar")
sync_app = typer.Typer(help="Sync assignments from OAKS/Connect/VHL/Blended (your own accounts).", no_args_is_help=False)
app.add_typer(sync_app, name="sync")

# Windows pipes default to cp1252, which cannot encode Spanish accents in
# course titles (a SPAN200 sync line would crash the whole report). UTF-8 with
# replacement never crashes and the terminal itself is UTF-8 anyway.
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console()
err_console = Console(stderr=True, style="bold red")

_CONFIG_OPT = typer.Option(None, "--config", help="Path to config.toml (default: search upward from cwd).")


def _core(config: Optional[Path]) -> Core:
    try:
        core = Core.load(config)
    except BrainError as e:
        err_console.print(f"CONFIG ERROR: {e}")
        raise typer.Exit(1)
    for w in core.config.warnings:
        console.print(f"[yellow]WARNING:[/yellow] {w}")
    return core


def _fail(e: BrainError) -> None:
    err_console.print(str(e))
    raise typer.Exit(1)


def _paste_oaks_session() -> dict:
    """Read a pasted OAKS cURL/headers block from stdin and parse its cookies."""
    from . import sync as syncmod

    console.print(
        "Log into [cyan]https://lms.cofc.edu[/cyan], then in DevTools > Network, "
        "right-click the top request > Copy > 'Copy as cURL' (or 'Copy request "
        "headers'). Paste it here, then press Enter on an empty line:")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return syncmod.parse_cookie_blob("\n".join(lines))


@app.command()
def init(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Where to write config.toml (default: ./config.toml)."),
    materials: Optional[Path] = typer.Option(
        None, "--materials",
        help="Where course files live (default: a materials/ folder beside the "
             "config). macOS installs point this at a Finder-visible folder."),
) -> None:
    """First-run setup: pick your AI, add your courses, and write a working
    config - no hand-editing. Made for a brand-new install."""
    from . import setup as setupmod
    from .config import load_config
    from .connectors import SessionStore

    console.print(Panel.fit(
        "[bold]Command Center setup[/bold]\n"
        "A few questions and you'll be ready to go.",
        border_style="cyan"))

    target = (config or Path("config.toml")).resolve()
    if target.exists() and not typer.confirm(
            f"{target} already exists. Overwrite it?", default=False):
        # Declining is the RIGHT answer when updating - it preserves their
        # courses, index and deadlines. But a config generated before
        # self-update existed has no update_url, and updates._manifest_url
        # reads that key with no fallback, so the people who already have the
        # app would be the only ones who never get an update. Add just that
        # one missing key; nothing else about their config is touched.
        added = setupmod.ensure_update_url(target)
        if added:
            console.print("Kept the existing config, and switched on automatic "
                          "updates for it.")
        else:
            console.print("Kept the existing config. Nothing changed.")
        raise typer.Exit(0)
    cfg_dir = target.parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    data_dir = cfg_dir / "data"

    # --- who ---
    name = typer.prompt("Your name").strip()

    # --- where (optional: greeting + weather) ---
    lat = lon = None
    loc_label = ""
    city = typer.prompt("Your city, for weather (blank to skip)",
                        default="", show_default=False).strip()
    if city:
        geo = setupmod.geocode(city)
        if geo and geo.get("latitude") is not None:
            lat, lon, loc_label = geo["latitude"], geo["longitude"], geo["label"]
            console.print(f"  Found [green]{loc_label}[/green]  ({lat}, {lon})")
        else:
            console.print("  [yellow]Couldn't find that city; skipping weather.[/yellow]")

    # --- which AI ---
    console.print("\n[bold]How should the AI answer your questions?[/bold]")
    console.print("  1) Gemini      - FREE tier, no Claude needed   [green](recommended)[/green]")
    console.print("  2) OpenAI      - your own API key (paid)")
    console.print("  3) Claude API  - your own Anthropic key (paid)")
    console.print("  4) Claude Code - if you already have a Claude subscription")
    backend = {"1": "gemini", "2": "openai", "3": "api", "4": "subscription"}.get(
        typer.prompt("Choose 1-4", default="1").strip(), "gemini")
    _, _, env_var, label, where = setupmod.backend_info(backend)
    console.print(f"  Using [green]{label}[/green]")
    if env_var:
        console.print(f"  Get a free/paid key at [cyan]{where}[/cyan]")
        if backend == "gemini":
            console.print("  [dim]Tip: use a PERSONAL Gmail - school accounts are "
                          "usually blocked from AI Studio. The free tier is plenty.[/dim]")
        # Mask the key only at a real terminal; getpass blocks on piped stdin,
        # so a non-interactive run (a script, a test) reads it visibly instead.
        # Loop until the key actually WORKS, or the student chooses to move
        # on. Saving an unverified key is how a friend gets "All set." and a
        # chat that fails days later with no idea which step was wrong.
        while True:
            key = typer.prompt(f"Paste your {env_var} (blank to add later)",
                               default="", show_default=False,
                               hide_input=_sys.stdin.isatty()).strip()
            if not key:
                console.print(f"  [yellow]No key yet - add {env_var} to .env "
                              f"before chatting.[/yellow]")
                break
            setupmod.write_env_key(cfg_dir / ".env", env_var, key)
            console.print(f"  Saved to {cfg_dir / '.env'} (keep this private; it stays out of git)")
            console.print("  [dim]Testing it with one real question...[/dim]")
            ok, why = setupmod.verify_backend(backend, key)
            if ok:
                console.print("  [green]The key works.[/green]")
                break
            console.print(f"  [red]That key did not work:[/red] {why[:160]}")
            if backend == "gemini":
                console.print("  [yellow]If you made it with a SCHOOL Google "
                              "account, that is almost always the cause - "
                              "AI Studio issues the key and then blocks every "
                              "request. Make a new one with a personal "
                              "Gmail.[/yellow]")
            if not typer.confirm("  Try a different key?", default=True):
                console.print("  [yellow]Keeping the key as saved. Run "
                              "[bold]brain doctor[/bold] once you have fixed "
                              "it.[/yellow]")
                break
    else:
        # No key to paste (the Claude Code path), but it can still be missing
        # the CLI it runs through, which fails exactly the same way later.
        ok, why = setupmod.verify_backend(backend)
        if not ok:
            console.print(f"  [red]Heads up:[/red] {why[:200]}")

    # --- courses ---
    console.print("\n[bold]Your courses[/bold]")
    codes: list[str] = []
    if typer.confirm(
            "Auto-detect from OAKS? (you'll paste your OAKS login once)", default=True):
        cookies = _paste_oaks_session()
        if cookies:
            store = SessionStore(data_dir)
            store.save("oaks", cookies)
            try:
                found = setupmod.discover_courses(
                    setupmod.fetch_enrollments(store.load("oaks")))
            except Exception as e:  # noqa: BLE001 - any reach/parse failure -> manual
                found = []
                console.print(f"  [yellow]Couldn't read courses from OAKS ({e}).[/yellow]")
            if found:
                console.print("  Found your current-term courses:")
                for c in found:
                    console.print(f"    [green]{c['code']}[/green]  {c['name']}")
                if typer.confirm("Add all of these?", default=True):
                    codes = [c["code"] for c in found]
        else:
            console.print("  [yellow]No session parsed; enter codes manually.[/yellow]")
    if not codes:
        raw = typer.prompt("Course codes, comma-separated (e.g. FINC313, SPAN200)")
        codes = [setupmod.normalize_code(x) for x in raw.split(",") if x.strip()]
    codes = sorted(set(codes))
    if not codes:
        err_console.print("Need at least one course."); raise typer.Exit(1)

    # --- Google Calendar (optional, personal account) ---
    console.print("\n[bold]Google Calendar[/bold] (optional)")
    console.print("  In Google Calendar > Settings > your calendar > "
                  "'Secret address in iCal format', copy the link. Your personal "
                  "events will show next to course deadlines.")
    gcal = typer.prompt("Paste your Google Calendar iCal link (blank to skip)",
                        default="", show_default=False).strip()
    if gcal and not gcal.lower().startswith(("http://", "https://", "webcal://")):
        console.print("  [yellow]That doesn't look like a calendar link; skipping.[/yellow]")
        gcal = ""

    # --- write folders + config ---
    mat_root = Path(materials).expanduser().resolve() if materials else cfg_dir / "materials"
    for code in codes:
        (mat_root / code).mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    # The deadline CSV OAKS sync appends to (config points [calendar].fixed_csv
    # here); create it with just the header so the first sync has a target.
    cal_csv = cfg_dir / "calendar" / "fixed.csv"
    cal_csv.parent.mkdir(parents=True, exist_ok=True)
    if not cal_csv.exists():
        cal_csv.write_text(setupmod.FIXED_CSV_HEADER, encoding="utf-8")
    target.write_text(
        setupmod.render_config(name=name, backend=backend, courses=codes,
                               latitude=lat, longitude=lon, location_label=loc_label,
                               gcal_ics_url=gcal,
                               materials_root=str(mat_root) if materials else ""),
        encoding="utf-8")
    try:
        load_config(target)          # fail loud now, not on first real command
    except BrainError as e:
        err_console.print(f"Generated config did not validate: {e}")
        raise typer.Exit(1)

    console.print(Panel.fit(
        f"[green]All set.[/green]  Wrote {target}\n\n"
        f"Courses: {', '.join(codes)}\n\n"
        f"Next:\n"
        f"  1. Drop each course's files into  {mat_root}\n"
        f"  2. [bold]brain index[/bold]      build the search index\n"
        f"  3. [bold]brain serve[/bold]      then open http://127.0.0.1:8177\n\n"
        f"For auto-syncing deadlines and grades, load the browser extension "
        f"in browser-extension/ (one-time).",
        border_style="green", title="Ready"))


@app.command()
def index(
    collection: Optional[str] = typer.Option(None, "--collection", "-c", help="Only this collection."),
    force: bool = typer.Option(False, "--force", help="Reindex even unchanged files."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Index collections into the local database and embedding store."""
    core = _core(config)
    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        console=console, transient=True,
    ) as progress:
        task = progress.add_task("indexing...", total=None)
        try:
            report = core.index(
                only=[collection] if collection else None,
                force=force,
                progress=lambda msg: progress.update(task, description=msg[:100]),
            )
        except BrainError as e:
            progress.stop()
            _fail(e)

    table = Table(title="Index results")
    for col_name in ("collection", "scanned", "indexed", "skipped", "removed", "chunks added"):
        table.add_column(col_name, justify="right" if col_name != "collection" else "left")
    for c in report.collections:
        table.add_row(c.collection, str(c.scanned), str(c.indexed), str(c.skipped),
                      str(c.removed), str(c.chunks_added))
    console.print(table)

    failures = report.total_failures
    if failures:
        console.print(Panel(
            "\n".join(f"{f.path}\n    {f.reason}" for f in failures),
            title=f"[red]{len(failures)} file(s) FAILED to index[/red]",
            border_style="red",
        ))


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    collection: str = typer.Option(GLOBAL_COLLECTION, "--collection", "-c",
                                   help="Collection name, or 'all' for every collection."),
    k: Optional[int] = typer.Option(None, "-k", help="Results per collection (default: settings.top_k)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Semantic search. Prints matches with scores; no API call."""
    core = _core(config)
    k = core.config.settings.top_k if k is None else k
    conn = core.open_db()
    empty: list[str] = []
    try:
        retriever = core.retriever(conn)
        if collection == GLOBAL_COLLECTION:
            per = retriever.search_global(query, k)
            hits = [h for hs in per.values() for h in hs]
            empty = retriever.empty_collections()
        else:
            core.config.collection(collection)  # loud unknown-name error
            hits = retriever.search_collection(query, collection, k)
    except BrainError as e:
        _fail(e)
    finally:
        conn.close()

    if empty:
        console.print(f"[yellow]Not searched (nothing indexed):[/yellow] {', '.join(empty)}")

    if not hits:
        console.print(f"[yellow]Nothing relevant indexed[/yellow] - no chunk scored >= "
                      f"{core.config.settings.similarity_floor} for that query.")
        # Exit 1, not 0: a refusal is not a successful search, and scripts
        # piping this need to be able to tell the difference.
        raise typer.Exit(1)

    hits.sort(key=lambda h: h.score, reverse=True)
    table = Table(title=f"{len(hits)} result(s)")
    table.add_column("score", justify="right")
    table.add_column("collection")
    table.add_column("source")
    table.add_column("locator")
    table.add_column("preview", max_width=60)
    for h in hits:
        preview = " ".join(h.text.split())[:120]
        table.add_row(f"{h.score:.3f}", h.collection, Path(h.source_path).name, h.locator, preview)
    console.print(table)


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question."),
    collection: str = typer.Option(..., "--collection", "-c",
                                   help="Collection name, or 'all' for global mode."),
    k: Optional[int] = typer.Option(None, "-k", help="Chunks to retrieve (per collection in global mode)."),
    model: Optional[str] = typer.Option(None, "--model", help="Model override."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Ask a question against a collection, streaming a cited answer."""
    core = _core(config)
    if collection != GLOBAL_COLLECTION:
        try:
            core.config.collection(collection)
        except BrainError as e:
            _fail(e)
    conn = core.open_db()
    try:
        try:
            prepared = core.prepare_ask(conn, question, collection, k=k, model=model)
        except AssistBlocked as e:
            console.print(Panel(str(e), title="[red]BLOCKED by assist_level[/red]", border_style="red"))
            raise typer.Exit(2)
        except (NoRelevantResults, EmptyIndexError) as e:
            console.print(Panel(str(e), title="[yellow]No relevant sources[/yellow]", border_style="yellow"))
            raise typer.Exit(1)
        except BrainError as e:
            _fail(e)

        for notice in prepared.notices():
            console.print(f"[yellow]{notice}[/yellow]")

        console.print(f"[dim]model: {prepared.model} | {len(prepared.citations)} sources[/dim]\n")
        try:
            for delta in core.stream_answer(prepared, model=model):
                # markup=False / highlight=False: model output is data, and
                # square-bracket citations must not be read as Rich tags.
                console.print(delta, end="", soft_wrap=True,
                              markup=False, highlight=False)
        except BrainError as e:
            _fail(e)
        except Exception as e:  # SDK auth/network/rate-limit errors, not tracebacks
            err_console.print(f"{type(e).__name__}: {e}")
            raise typer.Exit(1)
        console.print("\n")

        table = Table(title="Sources")
        table.add_column("n", justify="right")
        table.add_column("collection")
        table.add_column("source")
        table.add_column("locator")
        table.add_column("score", justify="right")
        for c in prepared.citations:
            table.add_row(f"[{c.n}]", c.collection, str(Path(c.source_path)), c.locator, f"{c.score:.3f}")
        console.print(table)
    finally:
        conn.close()


@app.command()
def collections(config: Optional[Path] = _CONFIG_OPT) -> None:
    """List collections with index stats and assist levels."""
    core = _core(config)
    conn = core.open_db()
    try:
        stats = core.collection_stats(conn)
    finally:
        conn.close()
    table = Table(title="Collections")
    table.add_column("name")
    table.add_column("assist_level")
    table.add_column("docs", justify="right")
    table.add_column("chunks", justify="right")
    table.add_column("last indexed")
    table.add_column("failures", justify="right")
    for s in stats:
        level_style = {"full": "green", "explain_only": "yellow", "off": "red"}[s["assist_level"]]
        table.add_row(
            f"[{s['color']}]{s['name']}[/]",
            f"[{level_style}]{s['assist_level']}[/]",
            str(s["doc_count"]), str(s["chunk_count"]),
            s["last_indexed"] or "never",
            str(len(s["failures"])) if s["failures"] else "-",
        )
    console.print(table)
    for s in stats:
        for m in s["missing_roots"]:
            console.print(f"[red]MISSING ROOT[/red] {s['name']}: {m}")


@app.command()
def update(
    check_only: bool = typer.Option(False, "--check", help="Only say whether an update exists."),
    apply_now: bool = typer.Option(False, "--apply", help="Install a downloaded update now."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Check for a new version, download it, and install it on next launch."""
    from . import updates

    core = _core(config)
    if apply_now:
        res = updates.apply_pending(core.config, log=lambda m: console.print(f"[dim]{m}[/dim]"))
        if res.get("applied"):
            console.print(f"[green]Updated to {res['version']}.[/green] "
                          f"Reopen Command Center to use it.")
        else:
            console.print(f"Nothing installed: {res.get('reason')}")
        return

    if not (core.config.settings.update_url or "").strip():
        console.print("Automatic updates are not configured for this copy "
                      "(no [settings] update_url), so there is nothing to check.")
        return

    waiting = updates.pending(core.config)
    if waiting:
        console.print(f"[green]Version {waiting['version']} is downloaded[/green] "
                      f"and installs the next time you open Command Center.")
        return

    console.print("Checking for updates...")
    avail = updates.check(core.config)
    if avail is None:
        console.print(f"[green]You are up to date[/green] "
                      f"(version {updates.current_version()}).")
        return
    console.print(f"Version [bold]{avail.version}[/bold] is available "
                  f"(you have {updates.current_version()}).")
    if avail.notes:
        console.print(f"  [dim]{avail.notes}[/dim]")
    if check_only:
        console.print("Run [bold]brain update[/bold] to download it.")
        return
    res = updates.stage(core.config, avail)
    if res.get("staged"):
        console.print(f"[green]Downloaded {res['version']}.[/green] "
                      f"It installs the next time you open Command Center.")
    else:
        err_console.print(f"Could not download it: {res.get('reason')}")
        raise typer.Exit(1)


@app.command()
def doctor(
    offline: bool = typer.Option(False, "--offline", help="Skip the checks that need internet."),
    save: Optional[Path] = typer.Option(None, "--save", help="Also write the report to this file."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Check that everything works and say how to fix what does not.

    Run this first whenever something is wrong. It writes a report you can
    send to whoever set you up - your keys, passwords and cookies are
    described but never printed, so it is safe to share.
    """
    from . import doctor as doc

    report = doc.run(config, offline=offline)

    style = {doc.OK: "green", doc.WARN: "yellow", doc.FAIL: "red", doc.INFO: "dim"}
    table = Table(title="Command Center checkup")
    table.add_column(""); table.add_column("check"); table.add_column("result")
    for c in report.checks:
        table.add_row(f"[{style[c.status]}]{c.status}[/]", c.name, c.detail)
    console.print(table)
    for c in report.checks:
        if c.fix and c.status in (doc.FAIL, doc.WARN):
            console.print(f"  [{style[c.status]}]{c.name}[/]: {c.fix}")

    if report.failures:
        console.print(f"\n[red]{len(report.failures)} problem(s).[/red] Fix the "
                      f"first one - the rest often clear up with it.")
    elif report.warnings:
        console.print("\n[green]Everything essential works.[/green] The yellow "
                      "lines are optional extras.")
    else:
        console.print("\n[green]Everything checks out.[/green]")

    # Default the saved copy next to the app's own data, so the "send me the
    # report" instruction has a stable answer even when nothing else works.
    out = Path(save) if save else None
    if out is None:
        try:
            from .config import find_config, load_config

            out = Path(load_config(find_config(config)).settings.data_dir) / "diagnostic.txt"
        except Exception:
            out = Path.home() / "command-center-diagnostic.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.to_text(), encoding="utf-8")
        console.print(f"[dim]Saved a copy to {out} - safe to send.[/dim]")
    except OSError as e:
        console.print(f"[yellow]Could not save the report: {e}[/yellow]")

    raise typer.Exit(1 if report.failures else 0)


@calendar_app.command("link")
def calendar_link(
    url: str = typer.Argument(..., help="Your Google Calendar 'Secret address in iCal format'."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Connect (or change) your personal Google Calendar, then import it.

    The setup wizard asks for this once, in the middle of a long install, and
    it is easy to miss - this adds it afterwards without redoing setup.
    """
    import re as _re

    from .config import find_config

    if not url.lower().startswith(("http://", "https://", "webcal://")):
        err_console.print("That does not look like a calendar link. In Google Calendar: "
                          "Settings > your calendar > 'Secret address in iCal format'.")
        raise typer.Exit(1)
    url = _re.sub(r"^webcal://", "https://", url, flags=_re.IGNORECASE)

    cfg_path = find_config(config)
    text = cfg_path.read_text(encoding="utf-8-sig")
    if "[calendar]" not in text:
        err_console.print(
            f"{cfg_path} has no [calendar] section, so there is nowhere to put this. "
            f"Re-run: brain init")
        raise typer.Exit(1)
    quoted = '"' + url.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if url in text:
        console.print("[dim]That calendar is already connected; re-importing.[/dim]")
    elif _re.search(r"^\s*ics_urls\s*=", text, _re.MULTILINE):
        # Append into the existing list rather than replacing it, so an OAKS
        # feed already subscribed there is not silently dropped.
        def _add(m):
            inner = m.group(1).strip()
            return f"ics_urls = [{inner + ', ' if inner else ''}{quoted}]"

        text = _re.sub(r"^\s*ics_urls\s*=\s*\[(.*?)\]", _add, text,
                       count=1, flags=_re.MULTILINE | _re.DOTALL)
    else:
        text = text.replace("[calendar]", f"[calendar]\nics_urls = [{quoted}]", 1)
    backup = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    backup.write_text(cfg_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    cfg_path.write_text(text, encoding="utf-8")

    try:
        core = _core(config)
    except Exception:
        cfg_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        err_console.print("That link made the config invalid; nothing was changed.")
        raise
    console.print(f"Connected. Importing {cfg_path.name} calendars...")
    report = core.calendar_import()
    for s in report.sources:
        mark = "[green]ok[/green]" if s.status == "ok" else f"[yellow]{s.status}[/yellow]"
        console.print(f"  {mark}  {s.source}: {s.stored} event(s)  {s.detail[:60]}")
    console.print(f"[green]Calendar now holds {report.total_stored} events.[/green]")


@calendar_app.command("import")
def calendar_import(config: Optional[Path] = _CONFIG_OPT) -> None:
    """(Re)import ICS files, the fixed CSV, and recurring rules."""
    core = _core(config)
    try:
        report = core.calendar_import()
    except BrainError as e:
        _fail(e)
    table = Table(title="Calendar import")
    table.add_column("source")
    table.add_column("detail")
    table.add_column("stored", justify="right")
    table.add_column("status")
    styles = {
        "ok": "[green]ok[/green]",
        "partial": "[yellow]errors (see below)[/yellow]",
        "failed": "[red]FAILED[/red]",
    }
    for s in report.sources:
        table.add_row(s.source, s.detail, str(s.stored), styles[s.status])
    console.print(table)
    for e in report.all_errors:
        console.print(f"[red]ERROR:[/red] {e}")
    for src in report.upsert_only():
        console.print(
            f"[yellow]'{src}' was updated in place, not rebuilt[/yellow] - because one of "
            f"its inputs failed, existing {src} events were kept rather than deleted. "
            f"Anything removed at the source may still be showing. Fix the errors above "
            f"and reimport for a clean rebuild."
        )
    console.print(f"Stored {report.total_stored} events total.")


@calendar_app.command("next")
def calendar_next(
    days: int = typer.Option(14, "--days", help="Look-ahead window in days."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Upcoming deadlines (exams, quizzes, projects) in the next N days."""
    from . import calendar as cal

    core = _core(config)
    conn = core.open_db()
    try:
        now = datetime.now()
        horizon = now + timedelta(days=days)
        # No LIMIT: capping before the window filter would silently drop
        # deadlines inside the requested range.
        events = [e for e in cal.next_events(conn, now, limit=None)
                  if datetime.fromisoformat(e["starts_at"]) <= horizon]
    finally:
        conn.close()
    if not events:
        console.print(f"No deadlines in the next {days} days. "
                      "(Run 'brain calendar import' if the calendar is empty.)")
        return
    table = Table(title=f"Next {days} days - {len(events)} deadline(s)")
    table.add_column("when")
    table.add_column("course")
    table.add_column("kind")
    table.add_column("title")
    for e in events:
        dt = datetime.fromisoformat(e["starts_at"])
        when = dt.strftime("%a %b %d") if e["all_day"] else dt.strftime("%a %b %d %I:%M %p")
        table.add_row(when, e["course"], e["kind"], e["title"])
    console.print(table)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (localhost only by design)."),
    port: int = typer.Option(8177, "--port"),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Start the web app (Today / Calendar / Chat / Library)."""
    _core(config)  # validate config loudly before uvicorn starts
    import os

    import uvicorn

    if config:
        os.environ["BRAIN_CONFIG"] = str(Path(config).resolve())
    console.print(f"Command Center on http://{host}:{port}")
    uvicorn.run("brain.web.app:create_app", host=host, port=port, factory=True)



# --------------------------------------------------------------- sync

def _sync_report(report, console):
    from rich.table import Table
    table = Table(title="Assignment sync")
    table.add_column("site"); table.add_column("status")
    table.add_column("new", justify="right"); table.add_column("moved", justify="right")
    for s in report.sites:
        if s.ok and s.recon:
            age = f" ({s.session_age_h:.0f}h old)" if s.session_age_h else ""
            table.add_row(s.label, f"[green]ok{age}[/green]",
                          str(len(s.recon.new)), str(len(s.recon.moved)))
        else:
            first = s.error.splitlines()[0] if s.error else "unknown"
            table.add_row(s.label, f"[yellow]{first}[/yellow]", "-", "-")
    console.print(table)
    for s in report.sites:
        if not (s.ok and s.recon):
            continue
        from rich.markup import escape

        for c in s.recon.new:
            i = c.item
            console.print(f"  [green]NEW[/green]  {i.date}  {i.course}  {escape(i.title[:56])}")
        for c in s.recon.moved:
            i = c.item
            if c.old_time:   # retime: same date, the TIME changed at the source
                span = f"{c.old_date} {c.old_time} -> {i.start_time or '?'}"
            else:
                span = f"{c.old_date} -> {i.date}"
            console.print(f"  [yellow]MOVED[/yellow] {span}  {i.course}  {escape(i.title[:48])}")


@sync_app.callback(invoke_without_command=True)
def sync_main(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Write new/moved items into fixed.csv and reimport."),
    site: Optional[str] = typer.Option(None, "--site", help="Only this site (oaks|connect|vhl|blended)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Check every connected site for new or moved assignments."""
    if ctx.invoked_subcommand is not None:
        return
    from . import sync as syncmod
    core = _core(config)
    conn = core.open_db()
    try:
        report = syncmod.run(core.config, conn, only=site, apply=apply)
    except BrainError as e:
        _fail(e)
    finally:
        conn.close()
    _sync_report(report, console)
    if apply and report.applied:
        core.calendar_import()
        console.print(f"[green]Applied {report.applied} item(s) and reimported the calendar.[/green]")
    elif report.total_new or report.total_moved:
        console.print(f"{report.total_new} new, {report.total_moved} moved. "
                      f"Re-run with --apply to add them to the calendar.")
    else:
        console.print("No new or moved assignments across connected sites.")


@sync_app.command("login")
def sync_login(
    site: str = typer.Argument(..., help="oaks | connect | vhl | blended"),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Store the browser session for a site (paste its cookies)."""
    from . import sync as syncmod
    from .connectors import SessionStore, get
    core = _core(config)
    try:
        conn_obj = get(site)
    except KeyError as e:
        err_console.print(str(e)); raise typer.Exit(1)
    console.print(f"[bold]{conn_obj.label}[/bold]")
    console.print(conn_obj.login_hint)
    console.print(
        "In DevTools > Network, RIGHT-CLICK the top request > Copy > "
        "'Copy request headers' (or 'Copy as cURL') - no need to find the "
        "Cookie header yourself. Paste it all here, then an empty line:"
    )
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    cookies = syncmod.parse_cookie_blob("\n".join(lines))
    if not cookies:
        err_console.print("No cookies parsed."); raise typer.Exit(1)
    store = SessionStore(core.config.settings.data_dir)
    saved = store.save(site, cookies)
    console.print(f"[green]Saved {len(cookies)} cookie(s) for '{site}'[/green] -> {saved}")


@sync_app.command("capture")
def sync_capture(
    site: str = typer.Argument(...),
    url: str = typer.Argument(..., help="A URL from the site that returns your assignments as JSON."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Fetch a URL with the stored session and save the raw response."""
    import time as _time
    from .connectors import SessionStore
    from .connectors import http as chttp
    core = _core(config)
    store = SessionStore(core.config.settings.data_dir)
    if not store.has(site):
        err_console.print(f"No session for '{site}'. Run: brain sync login {site}")
        raise typer.Exit(1)
    session = store.load(site)
    with chttp.client(session) as c:
        resp = c.get(url)
    out = core.config.settings.data_dir / "sessions" / f"{site}_capture_{int(_time.time())}.txt"
    header = f"# {url}\n# status {resp.status_code} {resp.headers.get('content-type', '')}\n\n"
    out.write_text(header + resp.text, encoding="utf-8")
    console.print(f"[green]Captured {len(resp.text)} bytes[/green] (HTTP {resp.status_code}) -> {out}")


@sync_app.command("status")
def sync_status(config: Optional[Path] = _CONFIG_OPT) -> None:
    """Show which sites have a stored session and how old it is."""
    from .connectors import SessionStore, REGISTRY
    from rich.table import Table
    core = _core(config)
    store = SessionStore(core.config.settings.data_dir)
    t = Table(title="Sync sessions")
    t.add_column("site"); t.add_column("label"); t.add_column("session")
    for name, c in REGISTRY.items():
        age = store.age_hours(name)
        state = f"{age:.0f}h old" if age is not None else "not captured"
        t.add_row(name, c.label, state)
    console.print(t)


@sync_app.command("files")
def sync_files(
    site: Optional[str] = typer.Option(None, "--site", help="Only this site (default: all file-capable)."),
    apply: bool = typer.Option(False, "--apply", help="Download and reindex (default: dry run)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Pull newly-uploaded course files into their collection folders.

    Dry run lists what WOULD download; --apply downloads new files under a
    _synced/ subfolder in each collection root and reindexes those collections.
    Files you already have (by name, anywhere in the collection) are skipped.
    """
    from rich.markup import escape
    from rich.table import Table

    from . import sync as syncmod
    core = _core(config)
    report = syncmod.pull_files(core.config, only=site, apply=apply)

    if not report.files and not report.errors:
        console.print("No file-capable sessions, or nothing to pull. "
                      "OAKS exposes files; run: brain sync login oaks")
        return

    t = Table(title="Course files")
    t.add_column("status"); t.add_column("course"); t.add_column("file")
    t.add_column("module", overflow="fold")
    shown = 0
    for f in report.files:
        if f.status == "skipped":
            continue
        color = {"downloaded": "green", "failed": "red"}.get(f.status, "yellow")
        label = f.status if apply else ("would download" if f.status == "downloaded" else f.status)
        t.add_row(f"[{color}]{label}[/{color}]", f.course,
                  escape(f.filename[:46]), escape(f.module_path[:40]))
        shown += 1
    if shown:
        console.print(t)
    console.print(
        f"[bold]{report.downloaded}[/bold] "
        f"{'downloaded' if apply else 'new (dry run)'}, "
        f"{report.skipped} already present."
    )
    for site_name, msg in report.errors:
        err_console.print(f"{site_name}: {msg.splitlines()[0]}")

    if apply and report.downloaded:
        touched = sorted({f.course for f in report.files if f.status == "downloaded"})
        console.print(f"Reindexing: {', '.join(touched)} ...")
        core.index(only=touched)
        console.print("[green]Done. New files are searchable in Chat.[/green]")
        # Pipeline: any just-downloaded file that is a scan (no text layer)
        # would otherwise index to zero chunks - transcribe it right away so
        # "pulled" always means "searchable".
        from . import ocr as ocrmod

        scans = [c for c in ocrmod.find_candidates(core.config)
                 if c.collection in touched]
        if scans:
            console.print(f"{len(scans)} new file(s) are image-only scans - "
                          f"transcribing so they index...")
            done = set()
            for c in scans:
                console.print(f"  [bold]{escape(c.path.name)}[/bold] ({c.pages}p)")
                try:
                    text = ocrmod.transcribe(c, core.config)
                except Exception as e:
                    err_console.print(f"    failed: {type(e).__name__}: {e} "
                                      f"(retry later: brain ocr --apply)")
                    continue
                if text.strip():
                    ocrmod.write_companion(c, text)
                    done.add(c.collection)
            if done:
                core.index(only=sorted(done))
                console.print("[green]Transcripts indexed.[/green]")
    elif not apply and report.downloaded:
        console.print("Re-run with [bold]--apply[/bold] to download and index them.")


@sync_app.command("news")
def sync_news(
    apply: bool = typer.Option(False, "--apply", help="Save new announcements + reindex (default: just list)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Check course announcements (OAKS news). New ones stay 'unread' until
    --apply saves them into the collection (searchable in Chat) and marks
    them read."""
    from rich.markup import escape

    from . import sync as syncmod
    core = _core(config)
    report = syncmod.check_news(core.config, apply=apply)
    for site, msg in report.errors:
        err_console.print(f"{site}: {msg}")
    if not report.new:
        console.print(f"[green]No new announcements[/green] "
                      f"({report.total} total, all seen).")
        return
    for n in report.new:
        console.print(f"  [bold]{n['date']}[/bold]  {n['course']}  "
                      f"{escape(n['title'][:64])}")
        preview = " ".join((n.get("text") or "").split())[:110]
        if preview:
            console.print(f"      [dim]{escape(preview)}[/dim]")
    if apply:
        touched = sorted({n["course"] for n in report.new})
        console.print(f"Saved {report.saved} announcement(s). "
                      f"Reindexing: {', '.join(touched)} ...")
        core.index(only=touched)
        console.print("[green]Done - announcements are searchable in Chat.[/green]")
    else:
        console.print(f"\n{len(report.new)} new. Re-run with [bold]--apply[/bold] "
                      f"to save them into the library.")


@sync_app.command("quizzes")
def sync_quizzes(
    apply: bool = typer.Option(False, "--apply", help="Save quiz text + reindex (default: just list)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Quiz descriptions/instructions from OAKS as searchable notes. The quiz
    record is where a posted study guide or format note lives; most quizzes
    have none, and those are skipped rather than saved empty."""
    from rich.markup import escape

    from . import sync as syncmod
    core = _core(config)
    report = syncmod.pull_quiz_content(core.config, apply=apply)
    for site, msg in report.errors:
        err_console.print(f"{site}: {msg}")
    changed = [q for q in report.quizzes if q["status"] != "unchanged"]
    if not changed:
        console.print(f"[green]No new quiz text[/green] "
                      f"({report.total} quizzes checked; "
                      f"{len(report.quizzes)} with text, all current).")
        return
    for q in changed:
        console.print(f"  [bold]{q['status']:>7}[/bold]  {q['course']}  "
                      f"{escape(q['name'][:64])}")
    if apply:
        touched = sorted({q["course"] for q in changed})
        console.print(f"Saved {report.saved} quiz note(s). "
                      f"Reindexing: {', '.join(touched)} ...")
        core.index(only=touched)
        console.print("[green]Done - quiz text is searchable in Chat.[/green]")
    else:
        console.print(f"\n{len(changed)} with new text. Re-run with "
                      f"[bold]--apply[/bold] to save them into the library.")


@app.command("grades")
def grades_cmd(
    refresh: bool = typer.Option(False, "--refresh", help="Fetch live from OAKS (default: cached)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Your gradebooks: per-course items, scores, and an honest summary."""
    from rich.markup import escape
    from rich.table import Table

    from . import grades as grades_mod
    core = _core(config)
    data = grades_mod.refresh(core.config) if refresh else grades_mod.load_cached(core.config)
    if not data.get("fetched_at"):
        console.print("No cached grades yet - fetching live...")
        data = grades_mod.refresh(core.config)
    for site, msg in data.get("errors", []):
        err_console.print(f"{site}: {msg}")
    if not data.get("courses"):
        console.print("No gradebooks reachable. Is the OAKS session alive? "
                      "(brain sync status)")
        return
    for c in data["courses"]:
        s = c["summary"]
        head = f"[bold]{c['course']}[/bold] - "
        if s["current_pct"] is not None:
            head += f"current {s['current_pct']}% ({s['basis']})"
        else:
            head += "nothing graded yet"
        head += f"  [{s['graded_count']}/{s['total_count']} items graded]"
        console.print(head)
        graded = [i for i in c["items"] if i.get("graded")]
        for i in graded:
            disp = f" ({i['displayed']})" if i.get("displayed") else ""
            console.print(f"    {escape(i['name'][:52]):54} "
                          f"{i['score']}/{i['out_of']}{disp}")
    import time as _t

    age_h = (_t.time() - data["fetched_at"]) / 3600.0
    console.print(f"[dim]fetched {age_h:.1f}h ago - refresh with: brain grades --refresh[/dim]")


@sync_app.command("links")
def sync_links(
    apply: bool = typer.Option(False, "--apply", help="Fetch + save + reindex (default: list)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Turn OAKS 'Link' materials (Google Docs, SharePoint, articles, videos)
    into searchable notes. Content is embedded where a session allows;
    everything else is still indexed as a titled link."""
    from rich.markup import escape
    from rich.table import Table

    from . import sync as syncmod
    core = _core(config)
    report = syncmod.pull_links(core.config, apply=apply)
    for site, msg in report.errors:
        err_console.print(f"{site}: {msg}")
    if not report.links:
        console.print("No links found (or OAKS session missing).")
        return
    t = Table(title="Course links")
    t.add_column("result"); t.add_column("course"); t.add_column("kind")
    t.add_column("title", overflow="fold")
    for l in report.links:
        color = {"content": "green", "failed": "red", "skipped": "yellow"}.get(l.status, "cyan")
        label = l.status if apply else ("would fetch" if l.status == "content" else "note")
        t.add_row(f"[{color}]{label}[/{color}]", l.course, l.kind, escape(l.title[:44]))
    console.print(t)
    console.print(f"[bold]{report.notes}[/bold] link(s), "
                  f"{report.with_content} with embedded content.")
    if apply and report.notes:
        touched = sorted({l.course for l in report.links if l.status in ("content", "note")})
        console.print(f"Reindexing: {', '.join(touched)} ...")
        core.index(only=touched)
        console.print("[green]Done - links are searchable in Chat.[/green]")
    elif not apply:
        console.print("Re-run with [bold]--apply[/bold] to fetch and index them. "
                      "Google Docs and SharePoint need the browser extension "
                      "logged into those accounts.")


@app.command("ocr")
def ocr_cmd(
    collection: Optional[str] = typer.Option(None, "--collection", help="Only this collection."),
    apply: bool = typer.Option(False, "--apply", help="Transcribe and index (default: list candidates)."),
    config: Optional[Path] = _CONFIG_OPT,
) -> None:
    """Transcribe image-only files (scanned PDFs, image-wrapper HTML) into
    searchable companion .md files using the vision model.

    Dry run lists what would be transcribed; --apply runs the transcription on
    the subscription backend and reindexes the touched collections. Files with
    an up-to-date "(transcribed).md" companion are skipped automatically.
    """
    from rich.markup import escape

    from . import ocr as ocrmod
    core = _core(config)
    cands = ocrmod.find_candidates(core.config, only=collection)
    if not cands:
        console.print("[green]Nothing to transcribe - every image-only file "
                      "already has an up-to-date transcript.[/green]")
        return
    for c in cands:
        console.print(f"  {escape(c.display)}")
    if not apply:
        console.print(f"\n{len(cands)} candidate(s). Re-run with "
                      f"[bold]--apply[/bold] to transcribe them.")
        return
    ok, reason = core.backend_status()
    if not ok:
        _fail(RuntimeError(f"Vision backend unavailable: {reason}"))
    touched: set[str] = set()
    for c in cands:
        console.print(f"[bold]Transcribing[/bold] {escape(c.path.name)} "
                      f"({c.pages} page(s))...")
        try:
            text = ocrmod.transcribe(
                c, core.config,
                progress=lambda msg: console.print(f"    {msg}"))
        except Exception as e:
            err_console.print(f"  failed: {type(e).__name__}: {e}")
            continue
        if not text.strip():
            err_console.print("  model returned nothing; not writing a companion.")
            continue
        out = ocrmod.write_companion(c, text)
        console.print(f"  [green]wrote[/green] {escape(out.name)} "
                      f"({len(text):,} chars)")
        touched.add(c.collection)
    if touched:
        names = sorted(touched)
        console.print(f"Reindexing: {', '.join(names)} ...")
        core.index(only=names)
        console.print("[green]Done. Transcripts are searchable in Chat.[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
