"""Render the live web app's pages to screenshots, HTML and plain text under
`<phase>/docs/walkthrough/<date>/`, with an `index.md` that says what each page is for
and what to look at.

A persona review reads the code, PR bodies and task files but never sees a page; this
captures the real UI so a designer, usability expert or user persona can judge it, and a
person can follow the index as a QA script. Screenshots use Playwright's Chromium when it
is available; with no browser the capture falls back to HTML and plain text only and says
so in the index.

The web app itself is untouched: pages are fetched from an in-process test client (or a
running server given its URL), so nothing here needs a port or a browser to produce the
HTML and text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .model import Phase
from .runs import RunStore
from .store import Store

Log = Callable[[str], None]


@dataclass
class PageSpec:
    """One page to capture: where it lives, what it is, and the one thing to look at."""

    slug: str
    url: str
    title: str
    purpose: str
    look: str


@dataclass
class PageResult:
    spec: PageSpec
    status: int
    html_bytes: int
    shot: bool = False
    note: str = ""


@dataclass
class WalkthroughResult:
    out_dir: Path
    pages: list[PageResult] = field(default_factory=list)
    screenshots: bool = False
    browser_note: str = ""
    include_stderr: bool = False


# --------------------------------------------------------------------------- page selection
def _first_closed(store: Store) -> tuple[str, str] | None:
    for prod in store.products():
        for ph in prod.phases:
            if ph.closed:
                return prod.name, ph.name
    return None


def _task_and_run(store: Store, phase: Phase) -> tuple[str, str]:
    """A task in this phase that has runs, and one of its run ids, for the task and run
    pages. Falls back to the phase's first task (or empty) when nothing has run yet."""
    runs = RunStore(store.config.garden_dir)
    task_id, run_id = "", ""
    for t in phase.tasks:
        rs = runs.runs_for(t.id)
        if rs:
            return t.id, rs[-1].run_id
        if not task_id:
            task_id = t.id
    return task_id, run_id


def pages_for(store: Store, phase: Phase) -> list[PageSpec]:
    """The pages to capture, in the order a person uses them, with the data this phase has."""
    key = phase.key
    specs = [
        PageSpec("now", "/", "Now",
                 "The first page: everything that needs the operator now.",
                 "Can a person immediately tell what needs action?"),
        PageSpec("inbox", "/inbox", "Inbox",
                 "What needs a decision and what is only a notice; the rail badge counts decisions only.",
                 "Is the split between a decision and a notice clear, and is the empty state designed?"),
        PageSpec("board", "/board", "Board (columns)",
                 "The board in columns, one per status in the loop's order.",
                 "Do the columns read left to right as the loop moves work?"),
        PageSpec("board-list", "/board?view=list", "Board (list)",
                 "The board as a list grouped by status, with a per-state fact on each row.",
                 "Does each row say enough to act without opening the task?"),
        PageSpec("trellis", "/trellis", "Trellis",
                 "The dependency and stacking graph with growth-stage glyphs and the hide-done control.",
                 "Can you follow what blocks what, and what the glyphs mean?"),
        PageSpec("phase", f"/phases/{key}", "Phase",
                 "The phase page: goals, the task table, budget and cost, persona reviews.",
                 "Is the important thing (what needs you) above the fold?"),
    ]
    task_id, run_id = _task_and_run(store, phase)
    if task_id:
        specs.append(PageSpec("task", f"/tasks/{task_id}", "Task",
                              "A task page: state, tier and priority controls, runs, the live log, the actions.",
                              "Are the controls and the run history legible, and is it clear what happens next?"))
    if task_id and run_id:
        specs.append(PageSpec("run", f"/runs/{task_id}/{run_id}", "Run",
                              "A run page: the transcript, the brief, the final message and stderr.",
                              "Can you tell what the worker did and why it ended as it did?"))
    specs.append(PageSpec("runs", "/runs", "Runs",
                          "Every run with its cost and tokens.",
                          "Is cost easy to total and attribute?"))
    specs.append(PageSpec("herbarium", "/herbarium", "Herbarium",
                          "A plate per phase; closed phases live here.",
                          "Does a closed phase read as a finished, catalogued thing?"))
    closed = _first_closed(store)
    if closed:
        specs.append(PageSpec("closed-phase", f"/phases/{closed[0]}/{closed[1]}", "Closed phase",
                              "A closed phase's header: the record of what it did, with no working controls.",
                              "Is it obviously a record, not a live board?"))
    specs.append(PageSpec("config", "/config", "Config",
                          "Configuration: pause and resume, live overrides, the tier map.",
                          "Are the live controls and their effect clear?"))
    specs.append(PageSpec("trials", "/trials", "Trials",
                          "The model leaderboard from every trial.",
                          "Does the ranking say which model to pick and why?"))
    specs.append(PageSpec("events", "/events", "Events",
                          "The event timeline.",
                          "Can you reconstruct what happened from the timeline alone?"))
    return specs


# --------------------------------------------------------------------------- html -> text
_BLOCK = re.compile(r"</(p|div|li|tr|h[1-6]|section|header|footer|article|table|ul|ol|nav|form)>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_DROP = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n[ \t]*\n[ \t]*\n+")

# The run page's stderr tab: raw process stderr can carry secrets a test suite printed,
# tracebacks or other things that should never land in a committed docs/ page.
_STDERR_NOTE = "(stderr omitted by garden walkthrough; rerun with --include-stderr to capture it)"
_STDERR_BLOCK = re.compile(
    r'(<div class="tab-panel" data-tab="stderr">).*?(</div>)', re.I | re.S,
)


def _scrub_stderr(page: str) -> str:
    return _STDERR_BLOCK.sub(rf'\1<pre class="log">{_STDERR_NOTE}</pre>\2', page)


def _redact_home(text: str, home: str) -> str:
    """Replace the capturing machine's home directory with `~` wherever it appears (worktree
    paths in briefs, transcripts and stderr are absolute and otherwise leak the operator's
    username and directory layout into a page committed to the garden repo)."""
    if not home or home == "/":
        return text
    return text.replace(home, "~")


def html_to_text(page: str) -> str:
    """A plain-text rendering that reads roughly as the page does, top to bottom: scripts
    and styles dropped, block ends turned into newlines, remaining tags stripped."""
    page = _DROP.sub("", page)
    page = _BR.sub("\n", page)
    page = _BLOCK.sub("\n", page)
    page = _TAG.sub("", page)
    page = html.unescape(page)
    page = "\n".join(line.rstrip() for line in page.splitlines())
    return _BLANKS.sub("\n\n", page).strip() + "\n"


# --------------------------------------------------------------------------- capture
def _fetch(store: Store, specs: list[PageSpec], base_url: str) -> dict[str, tuple[int, str]]:
    """GET each page. With a base_url, hit the running server; otherwise use an in-process
    test client of a fresh app (no port, no browser, works offline)."""
    out: dict[str, tuple[int, str]] = {}
    if base_url:
        import httpx

        with httpx.Client(base_url=base_url.rstrip("/"), timeout=30, follow_redirects=True) as c:
            for s in specs:
                r = c.get(s.url)
                out[s.slug] = (r.status_code, r.text)
        return out
    from fastapi.testclient import TestClient

    from .web.app import create_app

    with TestClient(create_app(store, watch=False)) as c:
        for s in specs:
            r = c.get(s.url, follow_redirects=True)
            out[s.slug] = (r.status_code, r.text)
    return out


VIEWPORTS = (1280, 390)
COLOR_SCHEMES = ("light", "dark")


def _screenshot(base_url: str, specs: list[PageSpec], out_dir: Path, log: Log) -> tuple[set[str], str]:
    """Render each page to a full-page PNG with Playwright's Chromium. Returns the set of
    slugs that got a screenshot and a note explaining any that did not."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set(), "Playwright is not installed (pip install 'context-garden[walkthrough]' && playwright install chromium)."
    shot: set[str] = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for s in specs:
                complete = True
                for width in VIEWPORTS:
                    for scheme in COLOR_SCHEMES:
                        page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
                        try:
                            page.goto(base_url.rstrip("/") + s.url, wait_until="networkidle", timeout=30000)
                            page.screenshot(path=str(out_dir / f"{s.slug}-{width}-{scheme}.png"), full_page=True)
                        except Exception as e:  # noqa: BLE001 - one bad page should not sink the rest
                            complete = False
                            log(f"  screenshot {s.slug} at {width}/{scheme} failed: {e}")
                        finally:
                            page.close()
                if complete:
                    shot.add(s.slug)
            browser.close()
    except Exception as e:  # noqa: BLE001 - a browser that will not launch (missing system libs)
        return shot, f"Chromium would not launch ({e}); run `playwright install chromium` or install its system libraries."
    return shot, ""


def _serve(store: Store) -> tuple[str, Callable[[], None]]:
    """Run the app on an ephemeral local port in a background thread (for the screenshots),
    returning its base URL and a stop callback."""
    import socket
    import threading
    import time

    import uvicorn

    from .web.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(create_app(store, watch=False, host="127.0.0.1", port=port), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=5)

    return f"http://127.0.0.1:{port}", stop


def capture(store: Store, phase: Phase, out_dir: Path, screenshots: bool = True,
            base_url: str = "", log: Log | None = None, include_stderr: bool = False) -> WalkthroughResult:
    """Write `<slug>.html`, `<slug>.txt` (and `<slug>.png` when a browser is available) for
    every page, plus `index.md`, under out_dir. Returns what was captured.

    Absolute home-directory paths are redacted to `~` in every page, and the run page's
    stderr tab is omitted unless `include_stderr` is set — this capture is committed to the
    garden repo, so it must not carry the operator's directory layout or raw process stderr
    (which can hold secrets a test suite printed or a traceback's local paths)."""
    log = log or (lambda _m: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = pages_for(store, phase)
    fetched = _fetch(store, specs, base_url)

    shot: set[str] = set()
    browser_note = ""
    if screenshots:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            browser_note = "Playwright is not installed (pip install 'context-garden[walkthrough]' && playwright install chromium)."
        else:
            if base_url:
                shot, browser_note = _screenshot(base_url, specs, out_dir, log)
            else:
                # Playwright drives a real browser, so it needs the app on a port, not a
                # test client: run it in a background thread just for the screenshots.
                url, stop = _serve(store)
                try:
                    shot, browser_note = _screenshot(url, specs, out_dir, log)
                finally:
                    stop()
        if browser_note:
            log(browser_note)

    result = WalkthroughResult(out_dir=out_dir, screenshots=bool(shot), browser_note=browser_note,
                               include_stderr=include_stderr)
    home = str(Path.home())
    for s in specs:
        status, page = fetched.get(s.slug, (0, ""))
        page = _redact_home(page, home)
        if not include_stderr:
            page = _scrub_stderr(page)
        (out_dir / f"{s.slug}.html").write_text(page)
        (out_dir / f"{s.slug}.txt").write_text(html_to_text(page))
        result.pages.append(PageResult(spec=s, status=status, html_bytes=len(page.encode()), shot=s.slug in shot))
        log(f"  {s.slug:<14} {s.url}  ({status}, {len(page.encode()) // 1024} KB){'  +png' if s.slug in shot else ''}")

    (out_dir / "index.md").write_text(_index_md(phase, result))
    return result


def _index_md(phase: Phase, result: WalkthroughResult) -> str:
    stamp = date.today().isoformat()
    out = [f"# Walkthrough of the live web app — {phase.key}, {stamp}", ""]
    if result.screenshots:
        out.append("Each page below has its purpose, one line on what to look at, a full-page "
                    "screenshot, the served HTML and a plain-text rendering (tags stripped, in "
                    "document order) that reads roughly as the page does top to bottom.")
    else:
        note = result.browser_note or "no browser was available"
        out.append("Screenshots were not captured (" + note + "). Each page below has its "
                   "purpose, one line on what to look at, the served HTML and a plain-text "
                   "rendering (tags stripped, in document order) that reads roughly as the page "
                   "does top to bottom.")
    out += ["", "Read the `.txt` for the words and the order; read the `.html` for structure, "
            "controls, forms, empty states and error text.", ""]
    if not result.include_stderr:
        out += ["Run page stderr is omitted (rerun `garden walkthrough` with --include-stderr to "
                "capture it); absolute home-directory paths are redacted to `~` throughout.", ""]
    else:
        out += ["Absolute home-directory paths are redacted to `~` throughout.", ""]
    for pr in result.pages:
        s = pr.spec
        out.append(f"## {s.title}: `{s.url}` (HTTP {pr.status}, {pr.html_bytes // 1024} KB)")
        out.append("")
        out.append(s.purpose)
        out.append("")
        out.append(f"Look at: {s.look}")
        out.append("")
        files = []
        if pr.shot:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    name = f"{s.slug}-{width}-{scheme}.png"
                    files.append(f"`{name}`")
                    out.append(f"![{s.title}, {width}px, {scheme}]({name})")
                    out.append("")
        files += [f"`{s.slug}.txt`", f"`{s.slug}.html`"]
        out.append("Files: " + ", ".join(files))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def ui_check(ctx: dict[str, object], spec: dict[str, object]) -> dict[str, object]:
    """Built-in pre-PR visual check. Capture into the check run, with an explicit warning
    and HTML/text artifacts when Chromium is unavailable."""
    garden_root = Path(str(spec["garden_root"]))
    out_dir = Path(str(spec["out_dir"]))
    store = Store(garden_root)
    phase = store.phase(str(ctx["product"]), str(ctx["phase"]))
    result = capture(store, phase, out_dir, screenshots=True)
    captures = [str(p) for p in sorted(out_dir.iterdir()) if p.suffix in {".png", ".html", ".txt", ".md"}]
    summary = f"captured {len(result.pages)} pages at 1280/390 in light/dark"
    if not result.screenshots:
        summary = f"HTML-only capture; {result.browser_note or 'browser unavailable'}"
    return {"status": "pass", "summary": summary, "details": result.browser_note,
            "captures": captures, "pages": [p.spec.slug for p in result.pages]}


# --------------------------------------------------------------------------- persona reading
def walkthrough_root(phase: Phase) -> Path:
    return phase.path / "docs" / "walkthrough"


def newest_walkthrough(phase: Phase) -> Path | None:
    """The most recent walkthrough directory that has an index.md, or None."""
    root = walkthrough_root(phase)
    if not root.exists():
        return None
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and (d / "index.md").exists()),
                  key=lambda d: d.name, reverse=True)
    return dirs[0] if dirs else None


def walkthrough_section(phase: Phase) -> str:
    """The persona-brief section pointing at (and inlining) the newest walkthrough, or ''."""
    d = newest_walkthrough(phase)
    if not d:
        return ""
    index = (d / "index.md").read_text().strip()
    return ("## Walkthrough of the live web app\n\n"
            f"A capture of the running web app for this phase is on disk at `{d}` "
            "(the served HTML and plain-text rendering of every page, with screenshots when a "
            "browser was available). Read the index below, then open the page files there before "
            "you judge the UI; quote what a person would actually see, not what a template could "
            "show.\n\n" + index)
