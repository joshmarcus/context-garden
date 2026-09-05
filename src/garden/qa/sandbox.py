"""The throwaway garden `garden qa` drives: a demo product whose repo is a local git repo
with a bare origin, the QA worker as its harness, an in-memory pretend GitHub, and the web
app served in a thread with every HTML page it renders recorded to disk."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..github import Feedback, PRInfo

WORKER = Path(__file__).with_name("worker.py")

# The seeded tasks. The `qa-worker:` line in each body reaches the worker inside its
# brief and picks what the fake does (see worker.py).
SEED_TASKS = [
    ("DM-001", "A worker that asks a question", "needs_input",
     "The worker stops half way to ask which database to use; the person answers on the task page."),
    ("DM-002", "A round with nothing to change", "no_change",
     "The first round finishes; a revise round finds nothing to change and says so."),
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=qa@example.com", "-c", "user.name=qa", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def make_garden(root: Path) -> Path:
    """Build the throwaway garden under `root` and return the garden directory."""
    root = root.resolve()  # the remote is named by path from inside the repo
    garden = root / "garden"
    repo = root / "repo"
    remote = root / "remote.git"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "qa"], cwd=repo, check=True)
    (repo / "README.md").write_text("# demo\n\nThe throwaway product `garden qa` works on.\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)

    garden.mkdir()
    worker = [sys.executable, str(WORKER)]
    (garden / "garden.yaml").write_text(yaml.safe_dump({
        "name": "qa",
        "max_parallel": 4,
        "max_attempts": 2,
        "max_revisions": 3,
        "timeout_minutes": 2,
        "tick_interval": 1,
        # The person presses the buttons: nothing starts on its own, so every dispatch,
        # revise run and resume is an action the agent took on a page.
        "auto_dispatch": False,
        "review": {"enabled": False},
        "plan": {"auto_approve": False},
        "github": {"draft_pr": True},
        "harnesses": {"claude": {"command": [*worker, "--model", "{model}"],
                                 "resume_command": [*worker, "--resume", "{session}"],
                                 "output": "claude-json", "resume": True,
                                 "models": {"easy": "qa-easy", "medium": "qa-medium", "hard": "qa-hard"}}},
        "products": {"demo": {"repo": "../repo", "base_branch": "main", "id_prefix": "DM", "github": "qa/demo"}},
    }, sort_keys=False))
    (garden / "principles").mkdir()
    (garden / "principles" / "00-index.md").write_text("# Digest\n\n- Keep the change small.\n")
    (garden / "demo").mkdir()
    (garden / "demo" / "product.md").write_text("# demo\n\nA demo product for `garden qa`.\n")
    phase = garden / "demo" / "p1"
    (phase / "tasks").mkdir(parents=True)
    (phase / "specs").mkdir()
    (phase / "goals.md").write_text("---\nplant: pea\nlatin: Pisum sativum\nplate: I\n---\n\n# p1 goals\n\nDrive the loop end to end.\n")
    (phase / "specs" / "spec.md").write_text("# spec\n\nNothing to it.\n")
    for tid, title, mode, why in SEED_TASKS:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        (phase / "tasks" / f"{tid}-{slug}.md").write_text(
            f"---\nid: {tid}\ntitle: {title}\nstatus: ready\ndepends_on: []\npriority: 1\ndifficulty: easy\nreading: []\n"
            f"created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n---\n\n"
            f"## Goal\n\n{why}\n\nqa-worker: {mode}\n"
        )
    return garden


# ---- the pretend GitHub -----------------------------------------------------------------

class MemoryGitHub:
    """An in-memory stand-in for `garden.github.GitHub`: PRs are numbered as they are opened,
    merging marks a PR merged (no branch moves), comments are kept for the page."""

    def __init__(self) -> None:
        self.available = True
        self.prs: dict[str, PRInfo] = {}  # head branch -> PR
        self.comments: dict[int, list[str]] = {}
        self._n = 0

    def describe(self) -> str:
        return "pretend GitHub (garden qa)"

    def me(self) -> str:
        return "qa-bot"

    def is_authenticated(self) -> bool:
        return True

    def _by_number(self, number: int) -> PRInfo:
        for pr in self.prs.values():
            if pr.number == number:
                return pr
        raise KeyError(number)

    def find_pr(self, slug: str, head_branch: str) -> PRInfo | None:
        return self.prs.get(head_branch)

    def get_pr(self, slug: str, number: int) -> PRInfo:
        return self._by_number(number)

    def create_pr(self, slug: str, head: str, base: str, title: str, body: str, draft: bool = False,
                  reviewers: list[str] | None = None) -> PRInfo:
        self._n += 1
        pr = PRInfo(number=self._n, url=f"/qa/github/pull/{self._n}", state="OPEN", title=title, head=head, base=base,
                    mergeable="MERGEABLE", updated_at=f"t{self._n}", body=body, is_draft=draft)
        self.prs[head] = pr
        return pr

    def feedback_since(self, slug: str, number: int, since_iso: str, exclude_logins: set[str] | None = None) -> Feedback:
        return Feedback()

    def update_pr(self, slug: str, number: int, title: str = "", body: str = "", base: str = "") -> None:
        pr = self._by_number(number)
        pr.title = title or pr.title
        pr.body = body or pr.body
        pr.base = base or pr.base

    def mark_ready(self, slug: str, number: int) -> None:
        self._by_number(number).is_draft = False

    def close_pr(self, slug: str, number: int) -> None:
        self._by_number(number).state = "CLOSED"

    def merge_pr(self, slug: str, number: int, method: str = "squash", delete_branch: bool = True) -> None:
        pr = self._by_number(number)
        if pr.state != "OPEN":
            raise RuntimeError(f"PR #{number} is {pr.state.lower()}, not open")
        if pr.is_draft:
            raise RuntimeError(f"PR #{number} is a draft; mark it ready for review first")
        pr.state = "MERGED"
        pr.updated_at += "m"

    def issue_comments(self, slug: str, number: int) -> list[str]:
        return list(self.comments.get(number, []))

    def comment(self, slug: str, number: int, body: str) -> None:
        self.comments.setdefault(number, []).append(body)


def register_pretend_github(app: Any, github: MemoryGitHub) -> None:
    """The pretend GitHub's two pages on the QA app: the PR list with a Merge button, and a
    PR's page. The person merges here, the way they would on GitHub; the next tick sees it."""
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse

    def page(title: str, body: str) -> str:
        return (f"<!doctype html><title>{html.escape(title)}</title><body style='font:14px system-ui;max-width:760px;margin:30px auto'>"
                f"<p><a href='/'>← back to the garden</a></p><h1>{html.escape(title)}</h1>"
                "<p>A pretend GitHub for <code>garden qa</code>: merging marks the pull request merged; no branch moves.</p>"
                + body)

    def row(pr: PRInfo) -> str:
        state = ("draft" if pr.is_draft and pr.state == "OPEN" else pr.state.lower())
        merge = (f"<form method='post' action='/qa/github/pull/{pr.number}/merge' style='display:inline'><button>Merge</button></form>"
                 if pr.state == "OPEN" and not pr.is_draft else "")
        return (f"<tr><td><a href='/qa/github/pull/{pr.number}'>#{pr.number}</a></td><td>{html.escape(pr.title)}</td>"
                f"<td><code>{html.escape(pr.head)}</code> → <code>{html.escape(pr.base)}</code></td><td>{state}</td><td>{merge}</td></tr>")

    @app.get("/qa/github", response_class=HTMLResponse)
    def pretend_github():
        rows = "".join(row(pr) for pr in sorted(github.prs.values(), key=lambda p: p.number))
        return page("Pull requests", "<table border='1' cellpadding='6'><tr><th>#</th><th>title</th><th>branches</th><th>state</th><th></th></tr>"
                    + (rows or "<tr><td colspan='5'>no pull requests yet</td></tr>") + "</table>")

    @app.get("/qa/github/pull/{number}", response_class=HTMLResponse)
    def pretend_pr(number: int):
        try:
            pr = github._by_number(number)
        except KeyError:
            raise HTTPException(404) from None
        comments = "".join(f"<li><pre style='white-space:pre-wrap'>{html.escape(c)}</pre></li>" for c in github.comments.get(number, []))
        return page(f"#{number} {pr.title}", f"<table border='1' cellpadding='6'>{row(pr)}</table>"
                    f"<h2>Description</h2><pre style='white-space:pre-wrap'>{html.escape(pr.body)}</pre>"
                    f"<h2>Comments</h2><ul>{comments or '<li>none</li>'}</ul>")

    @app.post("/qa/github/pull/{number}/merge")
    def pretend_merge(number: int):
        try:
            github.merge_pr("qa/demo", number)
        except KeyError:
            raise HTTPException(404) from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        return RedirectResponse("/qa/github", status_code=303)


# ---- page recording ---------------------------------------------------------------------

class PageRecorder:
    """ASGI middleware that writes every HTML page the app serves to `out/NNNN-<slug>.html`
    and keeps, per path, the latest file: the page the agent saw when it reported a finding."""

    def __init__(self, app: Any, out: Path) -> None:
        self.app = app
        self.out = out
        self.latest: dict[str, str] = {}  # path -> file name
        self._n = 0
        self._lock = threading.Lock()
        out.mkdir(parents=True, exist_ok=True)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        is_html = False

        async def send_wrapper(message: Any) -> None:
            nonlocal is_html
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers") or {}).get(b"content-type", b"")
                is_html = ctype.startswith(b"text/html")
            elif message["type"] == "http.response.body" and is_html:
                chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if is_html:
            path = scope.get("path", "/")
            query = scope.get("query_string", b"").decode()
            self.record(path + (f"?{query}" if query else ""), b"".join(chunks))

    def record(self, url: str, body: bytes) -> str:
        with self._lock:
            self._n += 1
            name = f"{self._n:04d}-{re.sub(r'[^a-z0-9]+', '-', url.lower()).strip('-') or 'index'}.html"
            (self.out / name).write_bytes(body)
            self.latest[url] = name
            self.latest[url.split("?", 1)[0]] = name
            return name

    def file_for(self, url: str) -> str:
        return self.latest.get(url) or self.latest.get(url.split("?", 1)[0], "")


# ---- the server -------------------------------------------------------------------------

@dataclass
class Sandbox:
    """A running throwaway garden: its directory, base URL, pretend GitHub and page record."""

    root: Path
    garden: Path
    github: MemoryGitHub
    pages: PageRecorder
    base_url: str = ""
    _server: Any = None
    _thread: threading.Thread | None = None
    _hub: Any = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def stop(self) -> None:
        if self._hub is not None:
            self._hub.stop()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def dump_log(self, path: Path) -> None:
        if self._hub is not None:
            path.write_text(json.dumps(self._hub.events, indent=1))


def start(root: Path, host: str = "127.0.0.1", port: int = 0) -> Sandbox:
    """Build the throwaway garden under `root` and serve it (with the scheduler loop) on a
    thread. Returns once the server accepts connections."""
    import uvicorn

    from ..store import Store
    from ..web.app import create_app

    garden = make_garden(root)
    github = MemoryGitHub()
    app = create_app(Store(garden), watch=True, github=github)
    register_pretend_github(app, github)
    recorder = PageRecorder(app, root / "pages")
    config = uvicorn.Config(recorder, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="garden-qa-server")
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline or not thread.is_alive():
            raise RuntimeError("the QA web server did not start")
        time.sleep(0.05)
    sock = server.servers[0].sockets[0].getsockname()
    box = Sandbox(root=root, garden=garden, github=github, pages=recorder, base_url=f"http://{sock[0]}:{sock[1]}")
    box._server, box._thread, box._hub = server, thread, app.state.hub
    return box
