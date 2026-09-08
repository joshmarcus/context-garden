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

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

from .browser import browser_failure, classify_browser_failure
from .model import Phase
from .runs import RunStore
from .scheduler import State
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
    browser_failure_kind: str = ""
    interaction_evidence: list[dict[str, object]] = field(default_factory=list)
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


def _decision_task(store: Store, phase: Phase) -> str:
    """Choose an open task whose page renders the same decision card a person must act on."""
    state = State(store.config.garden_dir / "state.json")
    fallback = ""
    for task in phase.tasks:
        if task.status.terminal:
            continue
        fallback = fallback or task.id
        facts = state.get(task.id)
        if (facts.get("decision") or facts.get("question") or facts.get("needs_human")
                or task.status.value in ("failed", "waiting_human")):
            return task.id
    return fallback


def _has_live_decision(store: Store, phase: Phase, task_id: str) -> bool:
    """Return whether a task already has decision state that the page can render."""
    task = next((task for task in phase.tasks if task.id == task_id), None)
    if task is None:
        return False
    facts = State(store.config.garden_dir / "state.json").get(task_id)
    return bool(facts.get("decision") or facts.get("question") or facts.get("needs_human")
                or task.status.value in ("failed", "waiting_human"))


def pages_for(store: Store, phase: Phase) -> list[PageSpec]:
    """The pages to capture, in the order a person uses them, with the data this phase has."""
    key = phase.key
    specs = [
        PageSpec("inbox", "/", "Inbox",
                 "The first page: everything that needs the operator now.",
                 "Can a person immediately tell what needs action?"),
        PageSpec("now", "/now", "Now",
                 "What is running, what is next, where the phase is and the last period, live from the events stream.",
                 "Can you say what the garden is doing and what comes next within five seconds?"),
        PageSpec("board", "/board", "Board (columns)",
                 "The board in columns, one per status in the loop's order.",
                 "Do the columns read left to right as the loop moves work?"),
        PageSpec("board-list", "/board?view=list", "Board (list)",
                 "The board as a list grouped by status, with a per-state fact on each row.",
                 "Does each row say enough to act without opening the task?"),
        PageSpec("backlog", "/board?view=backlog", "Backlog",
                 "The backlog view of work that is not yet ready to run.",
                 "Can you see what is waiting for approval or dependencies?"),
        PageSpec("trellis", "/trellis", "Trellis",
                 "The dependency and stacking graph with growth-stage glyphs and the hide-done control.",
                 "Can you follow what blocks what, and what the glyphs mean?"),
        PageSpec("phase", f"/phases/{key}", "Phase",
                 "The phase page: goals, the task table, budget and cost, persona reviews.",
                 "Is the important thing (what needs you) above the fold?"),
    ]
    design_root = _design_root(store, phase)
    if design_root.is_dir():
        first = next((p for p in sorted(design_root.rglob("*")) if p.is_file()), None)
        if first:
            rel = first.relative_to(design_root).as_posix()
            specs.append(PageSpec("design", f"/design/{rel}?product={phase.product}", "Design",
                                  "A product design document or mock served by the garden.",
                                  "Can a person open the design artifact directly from the app?"))
    task_id, run_id = _task_and_run(store, phase)
    decision_id = _decision_task(store, phase)
    if decision_id:
        has_live_decision = _has_live_decision(store, phase, decision_id)
        decision_url = f"/tasks/{decision_id}"
        if not has_live_decision:
            decision_url += "?walkthrough=decision"
        specs.append(PageSpec("task-decision", decision_url, "Task decision",
                              "A task page with an active worker decision or needs-you card.",
                              "Does the page explain the decision and give the person a clear recovery action?"))
    if task_id:
        specs.append(PageSpec("task", f"/tasks/{task_id}", "Task",
                              "A task page: state, tier and priority controls, runs, the live log, the actions.",
                              "Are the controls and the run history legible, and is it clear what happens next?"))
    if decision_id and decision_id != task_id:
        specs.append(PageSpec("task-ordinary", f"/tasks/{task_id}", "Task",
                              "An ordinary task page: state, runs, the live log and actions.",
                              "Are the controls and the run history legible, and is it clear what happens next?"))
    if task_id and run_id:
        specs.append(PageSpec("run", f"/runs/{task_id}/{run_id}", "Run",
                              "A run page: the transcript, the brief, the final message and stderr.",
                              "Can you tell what the worker did and why it ended as it did?"))
    specs.append(PageSpec("runs", "/runs", "Runs",
                          "Every run with its cost and tokens.",
                          "Is cost easy to total and attribute?"))
    specs.append(PageSpec("costs", "/costs", "Costs",
                          "Spend and accepted-task outcomes by activity, tier, model and harness.",
                          "Can you read what an accepted task costs and which route produced it?"))
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
    specs.append(PageSpec("retro", f"/phases/{key}/retro", "Retro",
                          "The phase retrospective, persona reports and filed follow-ups.",
                          "Can you see what the phase learned and what it carries forward?"))
    return specs


def _design_root(store: Store, phase: Phase) -> Path:
    """Locate the product checkout's design directory, including the self-product checkout."""
    configured = store.config.product_repo(phase.product)
    candidate = Path(configured) if not isinstance(configured, str) or "://" not in configured else phase.path.parent
    return candidate / "docs" / "design"


# --------------------------------------------------------------------------- html -> text
_BLANKS = re.compile(r"\n[ \t]*\n[ \t]*\n+")


class _TextParser(HTMLParser):
    """Collect visible text nodes without allowing markup attributes into the capture."""

    _BLOCK_TAGS = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                             "section", "header", "footer", "article", "table", "ul", "ol",
                             "nav", "form"})
    _IGNORED_TAGS = frozenset({"script", "style"})
    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                             "meta", "param", "source", "track", "wbr"})

    def __init__(self, hidden_selectors: list[tuple[str, bool, bool]] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_selectors = hidden_selectors or []
        self._elements: list[tuple[str, list[tuple[str, str | None]]]] = []
        self._hidden_depth = 0
        self._ignored_depth = 0
        self._hidden_starts: list[bool] = []
        self._ignored_starts: list[bool] = []

    @staticmethod
    def _matches_simple_selector(tag: str, attrs: list[tuple[str, str | None]], selector: str) -> bool:
        """Match the small, explicit selector subset used by the app's stylesheets.

        Returning false for syntax we do not understand is important here: this is a
        conservative visibility filter, not a CSS engine.  A false negative leaves text
        in a capture for review; a false positive can erase unrelated visible content.
        """
        values = {name.lower(): value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        index = 0
        tag_name = re.match(r"(?:[a-z][\w-]*|\*)", selector[index:], re.I)
        if tag_name:
            if tag_name.group(0).lower() not in ("*", tag.lower()):
                return False
            index += len(tag_name.group(0))
        while index < len(selector):
            marker = selector[index]
            if marker == "#":
                match = re.match(r"#[\w-]+", selector[index:])
                if not match or values.get("id") != match.group(0)[1:]:
                    return False
                index += len(match.group(0))
            elif marker == ".":
                match = re.match(r"\.[\w-]+", selector[index:])
                if not match or match.group(0)[1:] not in classes:
                    return False
                index += len(match.group(0))
            elif marker == "[":
                match = re.match(r"\[([\w-]+)(?:\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^\]\s]+)))?\]", selector[index:])
                if not match:
                    return False
                name = match.group(1).lower()
                expected = next((value for value in match.groups()[1:] if value is not None), None)
                if name not in values or (expected is not None and values[name] != expected):
                    return False
                index += len(match.group(0))
            elif selector.startswith(":not(", index):
                end = selector.find(")", index + 5)
                if end < 0:
                    return False
                if _TextParser._matches_simple_selector(tag, attrs, selector[index + 5:end]):
                    return False
                index = end + 1
            else:
                return False
        return True

    @staticmethod
    def _selector_components(selector: str) -> list[tuple[str, str | None]] | None:
        """Split selectors, retaining whether each component requires a direct parent."""
        components: list[tuple[str, str | None]] = []
        buffer: list[str] = []
        brackets = parentheses = 0
        pending: str | None = None
        whitespace = False

        def add_component() -> bool:
            nonlocal pending, whitespace
            component = "".join(buffer).strip()
            if not component:
                return True
            relation = pending
            if relation is None and components and whitespace:
                relation = " "
            components.append((component, relation))
            buffer.clear()
            pending = None
            whitespace = False
            return True

        for char in selector:
            if char == "[":
                brackets += 1
            elif char == "]":
                brackets -= 1
                if brackets < 0:
                    return None
            elif char == "(":
                parentheses += 1
            elif char == ")":
                parentheses -= 1
                if parentheses < 0:
                    return None
            if brackets or parentheses:
                buffer.append(char)
            elif char.isspace():
                add_component()
                whitespace = True
            elif char == ">":
                add_component()
                if not components or pending == ">":
                    return None
                pending = ">"
            else:
                if not buffer and whitespace and components and pending is None:
                    pending = " "
                buffer.append(char)
                whitespace = False
        if brackets or parentheses or not add_component() or pending:
            return None
        return components

    def _stylesheet_hidden(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if not self._hidden_selectors:
            return False
        # A selector's final component identifies the element; checking its ancestors
        # as well handles the descendant selectors used by the web templates without
        # needing a CSS dependency in the walkthrough tool.
        hidden: bool | None = None
        winning_rule: tuple[bool, int] | None = None
        for rule_index, (selector, is_none, important) in enumerate(self._hidden_selectors):
            components = self._selector_components(selector.strip())
            if not components:
                continue
            if not self._matches_simple_selector(tag, attrs, components[-1][0]):
                continue
            ancestors = self._elements
            index = len(ancestors) - 1
            matched = True
            for component_index in range(len(components) - 1, 0, -1):
                relation = components[component_index][1]
                component = components[component_index - 1][0]
                if relation == ">":
                    if index < 0 or not self._matches_simple_selector(*ancestors[index], component):
                        matched = False
                        break
                else:
                    while index >= 0 and not self._matches_simple_selector(*ancestors[index], component):
                        index -= 1
                    if index < 0:
                        matched = False
                        break
                index -= 1
            if matched:
                rule_order = (important, rule_index)
                if winning_rule is None or rule_order >= winning_rule:
                    winning_rule = rule_order
                    hidden = is_none
        return bool(hidden)

    def _is_hidden(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        values = {name.lower(): value for name, value in attrs}
        if "hidden" in values:
            return True
        if str(values.get("aria-hidden") or "").strip().lower() == "true":
            return True
        style = str(values.get("style") or "")
        return bool(re.search(r"(?:^|;)\s*display\s*:\s*none(?:\s*!important)?\s*(?:;|$)", style, re.I)) \
            or self._stylesheet_hidden(tag, attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        hidden = self._is_hidden(tag, attrs)
        ignored = tag in self._IGNORED_TAGS
        if tag in self._VOID_TAGS:
            if tag == "br" and not self._hidden_depth and not self._ignored_depth:
                self.parts.append("\n")
            return
        self._hidden_starts.append(hidden)
        self._ignored_starts.append(ignored)
        self._elements.append((tag, attrs))
        if hidden:
            self._hidden_depth += 1
        if ignored:
            self._ignored_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        hidden = self._hidden_starts.pop() if self._hidden_starts else False
        ignored = self._ignored_starts.pop() if self._ignored_starts else False
        if hidden:
            self._hidden_depth -= 1
        if ignored:
            self._ignored_depth -= 1
        if self._elements:
            self._elements.pop()
        if tag in self._BLOCK_TAGS and not self._hidden_depth and not self._ignored_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and not self._ignored_depth:
            self.parts.append(data)

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
    """Render visible element text, excluding hidden subtrees and all attributes."""
    hidden_selectors: list[tuple[str, bool, bool]] = []
    for css in re.findall(r"<style\b[^>]*>(.*?)</style\s*>", page, re.I | re.S):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            display = re.search(r"display\s*:\s*([\w-]+)(\s*!important)?", declarations, re.I)
            if display:
                is_none = display.group(1).lower() == "none"
                important = bool(display.group(2))
                hidden_selectors.extend(
                    (part.strip(), is_none, important)
                    for part in selectors.split(",")
                    if part.strip()
                )
    parser = _TextParser(hidden_selectors)
    parser.feed(page)
    parser.close()
    text = "".join(parser.parts)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip() + "\n"


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
NARROW_OUTER_WIDTH = 600
NARROW_FRAME_HEIGHT = 5400


class NarrowViewportError(RuntimeError):
    """The embedded page loaded, but did not fit the required narrow viewport."""

    def __init__(self, measurements: dict[str, int]) -> None:
        self.measurements = measurements
        super().__init__(
            "narrow frame measured "
            f"clientWidth {measurements['clientWidth']}, "
            f"scrollWidth {measurements['scrollWidth']}"
        )


def _narrow_frame(page: object, url: str) -> object:
    """Load a page in a 390px frame so Edge's outer-window floor cannot widen it."""
    import html

    frame_url = html.escape(url, quote=True)
    wrapper = ("<html><body style=\"margin:0\">"
               f"<iframe src=\"{frame_url}\" style=\"width:390px;height:{NARROW_FRAME_HEIGHT}px;border:0\"></iframe>"
               "</body></html>")
    page.set_content(wrapper, wait_until="networkidle", timeout=30000)
    iframe = page.locator("iframe")
    handle = getattr(iframe, "element_handle", lambda: None)()
    frame = handle.content_frame() if handle is not None else page.frame(url=url)
    if frame is None:
        raise RuntimeError(f"narrow frame did not load {url}")
    wait_for_load_state = getattr(frame, "wait_for_load_state", None)
    if wait_for_load_state is not None:
        wait_for_load_state("domcontentloaded", timeout=30000)
    measured = frame.evaluate(
        """() => {
            const width = document.documentElement.clientWidth;
            const scrollWidth = document.documentElement.scrollWidth;
            return {clientWidth: width, scrollWidth, scrollHeight: document.documentElement.scrollHeight};
        }"""
    )
    iframe.evaluate(
        "(iframe, height) => { iframe.style.height = `${Math.max(5400, height)}px`; }",
        measured["scrollHeight"],
    )
    if measured["clientWidth"] != 390 or measured["scrollWidth"] != 390:
        raise NarrowViewportError(measured)
    return {"clientWidth": measured["clientWidth"], "scrollWidth": measured["scrollWidth"]}


def _screenshot(base_url: str, specs: list[PageSpec], out_dir: Path, log: Log) -> tuple[set[str], dict[str, object] | None, list[dict[str, object]]]:
    """Render each page to a full-page PNG with Playwright's Chromium. Returns the set of
    slugs that got a screenshot and a note explaining any that did not."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set(), browser_failure("missing_playwright"), []
    shot: set[str] = set()
    evidence: list[dict[str, object]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for s in specs:
                complete = True
                for width in VIEWPORTS:
                    for scheme in COLOR_SCHEMES:
                        narrow = width == 390
                        page = browser.new_page(
                            viewport={"width": NARROW_OUTER_WIDTH if narrow else width,
                                      "height": 900},
                            color_scheme=scheme,
                        )
                        try:
                            url = base_url.rstrip("/") + s.url
                            if narrow:
                                measurements: dict[str, int] | None = None
                                try:
                                    measurements = _narrow_frame(page, url)
                                    log(f"  narrow frame {s.slug} {scheme}: "
                                        f"clientWidth={measurements['clientWidth']} "
                                        f"scrollWidth={measurements['scrollWidth']}")
                                except NarrowViewportError as e:
                                    complete = False
                                    log(f"  narrow frame {s.slug} at {scheme} failed: {e}")
                                except Exception as e:  # noqa: BLE001 - retain a load diagnostic
                                    complete = False
                                    log(f"  narrow frame {s.slug} at {scheme} failed: {e}")
                                finally:
                                    # Keep a diagnostic image when the page itself overflows;
                                    # the missing/invalid measurement must still fail the check.
                                    try:
                                        page.locator("iframe").screenshot(
                                            path=str(out_dir / f"{s.slug}-{width}-{scheme}.png"),
                                        )
                                    except Exception as e:  # noqa: BLE001 - outer handler logs it
                                        log(f"  diagnostic screenshot {s.slug} at {width}/{scheme} failed: {e}")
                                if measurements is not None:
                                    evidence.append({"page": s.slug, "action": "frame", "viewport": width,
                                                     "color_scheme": scheme, **measurements})
                            else:
                                response = page.goto(url, wait_until="networkidle", timeout=30000)
                                if response is None or not 200 <= response.status < 300:
                                    raise RuntimeError(f"HTTP {response.status if response else 'no response'}")
                                viewport = page.evaluate("""() => {
                                    if (!document.body || !document.body.innerHTML.trim()) throw new Error('empty document');
                                    return {clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth};
                                }""")
                                page.screenshot(path=str(out_dir / f"{s.slug}-{width}-{scheme}.png"), full_page=True)
                                evidence.append({"page": s.slug, "action": "navigate", "viewport": width,
                                                 "color_scheme": scheme, **viewport})
                        except Exception as e:  # noqa: BLE001 - one bad page should not sink the rest
                            complete = False
                            log(f"  screenshot {s.slug} at {width}/{scheme} failed: {e}")
                        finally:
                            page.close()
                if complete:
                    shot.add(s.slug)
            browser.close()
    except Exception as e:  # noqa: BLE001 - a browser that will not launch (missing system libs)
        kind, _action = classify_browser_failure(str(e))
        return shot, browser_failure(kind, str(e)), evidence
    return shot, None, evidence


def _prepare_browser() -> dict[str, object] | None:
    """Install Playwright's Chromium when its package is present but the browser is not.

    The browser is machine-local rather than a wheel payload. Both walkthroughs and PR UI
    checks come through this helper, so a prepared product environment needs no separate
    operator step. Return the installer's diagnostic when preparation fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return browser_failure("missing_playwright", str(exc))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return None
    except Exception as exc:  # noqa: BLE001 - a missing executable is the expected first-run case
        kind, _action = classify_browser_failure(str(exc))
        if kind != "missing_executable":
            return browser_failure(kind, str(exc))
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "Chromium installation failed").strip()[-1000:]
            return browser_failure("missing_executable", detail)
        return None


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
            base_url: str = "", log: Log | None = None, include_stderr: bool = False,
            pages: list[str] | None = None) -> WalkthroughResult:
    """Write `<slug>.html`, `<slug>.txt` (and `<slug>.png` when a browser is available) for
    each selected page (or every page when ``pages`` is omitted), plus `index.md`, under out_dir.
    Returns what was captured.

    Absolute home-directory paths are redacted to `~` in every page, and the run page's
    stderr tab is omitted unless `include_stderr` is set — this capture is committed to the
    garden repo, so it must not carry the operator's directory layout or raw process stderr
    (which can hold secrets a test suite printed or a traceback's local paths)."""
    log = log or (lambda _m: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = pages_for(store, phase)
    # ``None`` is the milestone-walkthrough default.  An explicit empty selection is
    # a scoped PR check with no rendered pages, not an accidental request for all of them.
    if pages is not None and "*" not in pages:
        specs = [spec for spec in specs if spec.slug in pages]
    fetched = _fetch(store, specs, base_url)

    shot: set[str] = set()
    failure: dict[str, object] | None = None
    interaction_evidence: list[dict[str, object]] = []
    if screenshots:
        failure = _prepare_browser()
        if not failure:
            if base_url:
                shot, failure, interaction_evidence = _screenshot(base_url, specs, out_dir, log)
            else:
                # Playwright drives a real browser, so it needs the app on a port, not a
                # test client: run it in a background thread just for the screenshots.
                url, stop = _serve(store)
                try:
                    shot, failure, interaction_evidence = _screenshot(url, specs, out_dir, log)
                finally:
                    stop()
        if failure:
            log(str(failure.get("diagnostic") or ""))

    result = WalkthroughResult(out_dir=out_dir, screenshots=bool(shot),
                               browser_note=str((failure or {}).get("diagnostic") or ""),
                               browser_failure_kind=str((failure or {}).get("kind") or ""),
                               interaction_evidence=interaction_evidence,
                               include_stderr=include_stderr)
    home = str(Path.home())
    for s in specs:
        status, page = fetched.get(s.slug, (0, ""))
        page = _redact_home(page, home)
        if not include_stderr:
            page = _scrub_stderr(page)
        (out_dir / f"{s.slug}.html").write_text(page)
        (out_dir / f"{s.slug}.txt").write_text(html_to_text(page))
        note = ""
        if not (200 <= status < 300) or not page.strip():
            note = "empty or unsuccessful document"
        result.pages.append(PageResult(spec=s, status=status, html_bytes=len(page.encode()), shot=s.slug in shot, note=note))
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


def _seeded_ui_capture(out_dir: Path, pages: list[str] | None = None) -> dict[str, object]:
    """Render the stable QA garden using the code imported from the proposed worktree."""
    from .qa.sandbox import make_garden
    from .scheduler import State

    with tempfile.TemporaryDirectory(prefix="garden-ui-") as scratch:
        garden_root = make_garden(Path(scratch))
        store = Store(garden_root)
        # Keep the visual fixture representative even before a worker has run: the
        # walkthrough must always give personas a real decision card to inspect.
        state = State(store.config.garden_dir / "state.json")
        state.get("DM-001")["decision"] = {
            "kind": "changed_outcome",
            "reason": "The worker needs a product decision before it can continue.",
        }
        state.save()
        logs: list[str] = []
        result = capture(store, store.phase("demo", "p1"), out_dir, screenshots=True,
                         log=logs.append, pages=pages)
    decision = next((page for page in result.pages if page.spec.slug == "task-decision"), None)
    decision_html = (out_dir / "task-decision.html").read_text() if decision else ""
    if decision is None or "class=\"panel decision-card\"" not in decision_html:
        return {"status": "fail", "summary": "decision-card walkthrough page is missing",
                "failure_kind": "product", "details": "task-decision.html must contain .decision-card",
                "captures": [], "interaction_evidence": [], "pages": [p.spec.slug for p in result.pages]}
    expected = {
        f"{page.spec.slug}-{width}-{scheme}.png"
        for page in result.pages
        for width in VIEWPORTS
        for scheme in COLOR_SCHEMES
    }
    missing = sorted(name for name in expected if not (out_dir / name).is_file())
    captures = [str(p) for p in sorted(out_dir.iterdir())
                if p.suffix in {".png", ".html", ".txt", ".md"}]
    expected = len(result.pages) * len(VIEWPORTS) * len(COLOR_SCHEMES)
    pngs = [path for path in captures if path.endswith(".png")]
    complete_pngs = result.screenshots and len(pngs) == expected
    evidence_complete = len(result.interaction_evidence) == expected
    summary = f"captured {len(result.pages)} pages at 1280/390 in light/dark"
    details = "\n".join(filter(None, [result.browser_note, *logs]))
    if not complete_pngs or missing:
        summary = f"UI check did not produce all PNGs ({len(pngs)}/{expected})"
        if missing:
            details = "\n".join(filter(None, [details, "missing PNGs: " + ", ".join(missing)]))
    elif not evidence_complete:
        summary = f"PNGs exist but executed interaction/viewport evidence is incomplete ({len(result.interaction_evidence)}/{expected})"
    passed = complete_pngs and not missing and evidence_complete
    return {"status": "pass" if passed else "fail", "summary": summary,
            "failure_kind": "infrastructure" if result.browser_failure_kind else
                            ("product" if not passed else ""),
            "browser_failure_kind": result.browser_failure_kind,
            "details": details or ("missing screenshot files or interaction evidence" if not passed else ""),
            "captures": captures, "interaction_evidence": result.interaction_evidence,
            "pages": [p.spec.slug for p in result.pages]}


def ui_check(ctx: dict[str, object], spec: dict[str, object]) -> dict[str, object]:
    """Run the visual check with the proposed worktree's package and templates.

    Python checks normally execute inside the scheduler process. An isolated subprocess with
    the worktree's ``src`` first on PYTHONPATH prevents an installed scheduler version from
    rendering its own templates, while the disposable QA garden makes page data deterministic.
    """
    out_dir = Path(str(spec["out_dir"]))
    worktree = Path(str(spec.get("worktree") or ctx.get("worktree") or ""))
    source = worktree / "src"
    if not source.is_dir():
        return {"status": "error", "summary": "UI check worktree source is missing", "details": str(source)}
    env = dict(os.environ)
    # This renderer is deliberately sourced wholly from the checkout under review.  In
    # particular, a pull-based worker may have been launched with a controller-local
    # PYTHONPATH that does not exist (or is not traversable) on the independent host.
    # Retaining that path can make Python fail while resolving modules even though the
    # checkout's source is first.
    env["PYTHONPATH"] = str(source)
    proc = subprocess.run(
        [sys.executable, "-m", "garden.walkthrough", "--ui-check", str(out_dir),
         json.dumps(spec.get("pages") or [])],
        cwd=worktree, env=env, capture_output=True, text=True, timeout=600, check=False,
    )
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"status": "error", "summary": "UI renderer did not return a result",
                "details": (proc.stderr or proc.stdout)[-2000:]}
    if proc.returncode:
        result["status"] = "error"
        result["details"] = (str(result.get("details") or "") + "\n" + proc.stderr).strip()[-2000:]
    return result


def _main() -> int:
    # Newer controllers supply optional page selection. This capture engine safely renders
    # all pages when no selection is supplied, preserving compatibility across pinned releases.
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--ui-check":
        pages = json.loads(sys.argv[3]) if len(sys.argv) == 4 else []
        print(json.dumps(_seeded_ui_capture(Path(sys.argv[2]), pages)))
        return 0
    return 2


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


if __name__ == "__main__":
    raise SystemExit(_main())
