"""The flows `garden qa` drives, as one table. Each flow is a name, the page it happens on,
the script in words (what the agent is told to do) and the same steps as code (what the
built-in scripted agent does). One source: the brief for a real agent and the scripted
run are generated from the same rows, so they cannot drift apart."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


class FlowFailed(Exception):
    """A step could not be completed; the message names what was expected and what was seen."""


class Client:
    """HTTP against the throwaway garden, the way a person's browser does it: forms are
    posted, the redirect is followed by hand, a flash in the redirect means the action was
    refused and the message is the failure."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.http = httpx.Client(base_url=base_url, follow_redirects=False, timeout=10.0)
        self.timeout = timeout
        self.last_page = "/"

    def close(self) -> None:
        self.http.close()

    def get(self, path: str) -> str:
        self.last_page = path
        r = self.http.get(path)
        if r.status_code != 200:
            raise FlowFailed(f"GET {path} returned {r.status_code}")
        return r.text

    def post(self, path: str, data: dict[str, str] | None = None, referer: str = "") -> str:
        """Post a form; return the page it redirected to. A flash on that page is a refusal."""
        headers = {"referer": self.http.base_url.join(referer or self.last_page).__str__()}
        r = self.http.post(path, data=data or {}, headers=headers)
        if r.status_code not in (303, 302):
            raise FlowFailed(f"POST {path} returned {r.status_code}: {r.text[:200]}")
        location = r.headers.get("location", "/")
        parts = urlsplit(location)
        flash = parse_qs(parts.query).get("flash", [""])[0]
        if flash:
            raise FlowFailed(f"POST {path} was refused: {flash}")
        self.last_page = parts.path
        return location

    def tasks(self) -> dict[str, dict[str, Any]]:
        r = self.http.get("/api/tasks")
        if r.status_code != 200:
            raise FlowFailed(f"GET /api/tasks returned {r.status_code}")
        return {t["id"]: t for t in r.json()}

    def status(self, task_id: str) -> str:
        t = self.tasks().get(task_id)
        return str(t["status"]) if t else ""

    def wait_status(self, task_id: str, *want: str) -> str:
        """Wait for a task to reach one of `want`, pressing "Tick now" between looks the way
        a person would rather than waiting a whole tick interval."""
        deadline = time.time() + self.timeout
        seen = self.status(task_id)
        while time.time() < deadline:
            seen = self.status(task_id)
            if seen in want:
                return seen
            self.http.post("/tick", headers={"referer": str(self.http.base_url.join(self.last_page))})
            time.sleep(0.2)
        raise FlowFailed(f"{task_id} is {seen or 'missing'}; expected {' or '.join(want)} within {self.timeout:.0f}s")

    def wait_for(self, what: str, check: Callable[[], bool]) -> None:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.2)
        raise FlowFailed(f"{what} did not happen within {self.timeout:.0f}s")


@dataclass(frozen=True)
class Flow:
    name: str
    page: str
    script: str
    run: Callable[[Client], None]


# ---- the steps --------------------------------------------------------------------------

def add_a_task(c: Client) -> None:
    before = set(c.tasks())
    c.get("/phases/demo/p1")
    c.post("/phases/demo/p1/plan", {"guidance": ""})
    c.wait_for("the planner's new tasks", lambda: len(set(c.tasks()) - before) >= 2)
    new = sorted(set(c.tasks()) - before)
    page = c.get("/phases/demo/p1")
    for tid in new:
        if tid not in page:
            raise FlowFailed(f"{tid} was planned but is not on the phase page")
    if c.status("DM-003") != "draft":
        raise FlowFailed(f"planned task DM-003 is {c.status('DM-003')}, expected draft (plan.auto_approve is off)")


def approve(c: Client) -> None:
    c.get("/tasks/DM-003")
    c.post("/tasks/DM-003/approve")
    if c.status("DM-003") != "ready":
        raise FlowFailed(f"DM-003 is {c.status('DM-003')} after Approve, expected ready")
    c.get("/phases/demo/p1")
    c.post("/phases/demo/p1/approve-all")
    if c.status("DM-004") != "ready":
        raise FlowFailed(f"DM-004 is {c.status('DM-004')} after Approve all drafts, expected ready")


def dispatch(c: Client) -> None:
    c.get("/tasks/DM-003")
    c.post("/tasks/DM-003/dispatch")
    if c.status("DM-003") != "running":
        raise FlowFailed(f"DM-003 is {c.status('DM-003')} after Dispatch now, expected running")
    c.wait_status("DM-003", "awaiting_triage")
    page = c.get("/tasks/DM-003")
    if "/qa/github/pull/" not in page:
        raise FlowFailed("DM-003 reached awaiting_triage but its page shows no pull request link")


def answer_a_question(c: Client) -> None:
    c.get("/tasks/DM-001")
    c.post("/tasks/DM-001/dispatch")
    c.wait_status("DM-001", "waiting_human")
    page = c.get("/tasks/DM-001")
    if "Postgres or SQLite?" not in page:
        raise FlowFailed("DM-001 is waiting for a person but its page does not show the worker's question")
    if "Postgres or SQLite?" not in c.get("/"):
        raise FlowFailed("the Inbox does not show DM-001's question")
    c.post("/tasks/DM-001/answer", {"note": "SQLite"})
    if c.status("DM-001") != "running":
        raise FlowFailed(f"DM-001 is {c.status('DM-001')} after the answer, expected running (resumed)")
    c.wait_status("DM-001", "awaiting_triage")
    page = c.get("/tasks/DM-001")
    if "SQLite" not in page:
        raise FlowFailed("the answer is not shown on DM-001's page after the worker resumed")


def send_back(c: Client) -> None:
    c.get("/tasks/DM-003")
    c.post("/tasks/DM-003/triage-changes", {"note": "tighten the tests"})
    if c.status("DM-003") != "changes_requested":
        raise FlowFailed(f"DM-003 is {c.status('DM-003')} after Send back, expected changes_requested")
    page = c.get("/tasks/DM-003")
    if "Dispatch revise run" not in page:
        raise FlowFailed("DM-003 is changes_requested but its page offers no 'Dispatch revise run' button")
    c.post("/tasks/DM-003/dispatch")
    c.wait_status("DM-003", "awaiting_triage")
    if "tighten the tests" not in c.get("/tasks/DM-003"):
        raise FlowFailed("the note sent back with DM-003 is not on its page")


def triage(c: Client) -> None:
    home = c.get("/")
    if "Ready for review" not in home:
        raise FlowFailed("the Inbox shows no 'Ready for review' button for the draft PRs awaiting triage")
    c.post("/tasks/DM-003/triage-ready", referer="/")
    if c.status("DM-003") != "in_review":
        raise FlowFailed(f"DM-003 is {c.status('DM-003')} after Ready for review, expected in_review")
    c.post("/tasks/DM-001/triage-ready", referer="/")
    if c.status("DM-001") != "in_review":
        raise FlowFailed(f"DM-001 is {c.status('DM-001')} after Ready for review, expected in_review")


def accept_no_change(c: Client) -> None:
    c.get("/tasks/DM-002")
    c.post("/tasks/DM-002/dispatch")
    c.wait_status("DM-002", "awaiting_triage")
    c.post("/tasks/DM-002/triage-changes", {"note": "please look again"})
    c.post("/tasks/DM-002/dispatch")
    c.wait_status("DM-002", "waiting_human")
    page = c.get("/tasks/DM-002")
    if "nothing to change" not in page.lower() and "no_change" not in page:
        raise FlowFailed("DM-002 is waiting for a person but its page does not present the nothing-to-change card")
    if "Accept" not in c.get("/"):
        raise FlowFailed("the Inbox shows no Accept button for DM-002's nothing-to-change card")
    c.post("/tasks/DM-002/accept", referer="/")
    c.wait_status("DM-002", "awaiting_triage", "in_review")
    c.post("/tasks/DM-002/triage-ready")
    if c.status("DM-002") != "in_review":
        raise FlowFailed(f"DM-002 is {c.status('DM-002')} after Ready for review, expected in_review")


def merge(c: Client) -> None:
    page = c.get("/qa/github")
    for tid in ("DM-001", "DM-002", "DM-003"):
        pr = str(c.tasks()[tid].get("pr") or "")
        if not pr.startswith("/qa/github/pull/"):
            raise FlowFailed(f"{tid} has no pull request link to the pretend GitHub (pr={pr!r})")
        if f"href='{pr}'" not in page:
            raise FlowFailed(f"{tid}'s pull request {pr} is not listed on the pretend GitHub")
        c.post(f"{pr}/merge", referer="/qa/github")
    for tid in ("DM-001", "DM-002", "DM-003"):
        c.wait_status(tid, "done")
    if "merged" not in c.get("/tasks/DM-003").lower():
        raise FlowFailed("DM-003 is done but its page does not say the PR merged")


def close_a_phase(c: Client) -> None:
    c.get("/tasks/DM-004")
    c.post("/tasks/DM-004/cancel")
    if c.status("DM-004") != "cancelled":
        raise FlowFailed(f"DM-004 is {c.status('DM-004')} after Cancel, expected cancelled")
    page = c.get("/phases/demo/p1")
    if "Close phase" not in page:
        raise FlowFailed("the phase page offers no 'Close phase' button")
    c.post("/phases/demo/p1/close")
    page = c.get("/phases/demo/p1")
    if "closed" not in page.lower():
        raise FlowFailed("demo/p1 was closed but its page does not say so")
    if "p1" not in c.get("/herbarium"):
        raise FlowFailed("the closed phase is not in the herbarium")


FLOWS: list[Flow] = [
    Flow("add a task", "/phases/demo/p1",
         "On the phase page press 'Plan phase' (the planner is a fake and returns two tasks). "
         "Wait until the two new tasks appear in the table as drafts (DM-003 and DM-004).", add_a_task),
    Flow("approve", "/tasks/DM-003",
         "Open DM-003 and press Approve; it becomes ready. Then on the phase page press 'Approve all drafts' so DM-004 is ready too.", approve),
    Flow("dispatch", "/tasks/DM-003",
         "On DM-003 press 'Dispatch now'. The worker runs and finishes within seconds; the loop reaps it on the next tick "
         "(press '↻ Tick now' in the rail if you do not want to wait). DM-003 ends up awaiting triage with a draft PR link.", dispatch),
    Flow("answer a worker's question", "/tasks/DM-001",
         "Dispatch DM-001. Its worker stops to ask 'Postgres or SQLite?': the task waits for you and the question shows on "
         "its page and in the Inbox. Answer 'SQLite' with 'Answer and resume'; the worker resumes and the task reaches awaiting triage.", answer_a_question),
    Flow("send back with a note", "/tasks/DM-003",
         "On DM-003 (awaiting triage) use 'Send back' with the note 'tighten the tests'. It becomes changes requested; "
         "press 'Dispatch revise run'. It comes back awaiting triage and the note is on its page.", send_back),
    Flow("triage", "/inbox",
         "In the Inbox press 'Ready for review' on DM-003 and on DM-001; both become in review.", triage),
    Flow("accept a nothing-to-change card", "/tasks/DM-002",
         "Dispatch DM-002 and wait for awaiting triage. Send it back with a note and dispatch the revise run: this worker "
         "reports that there is nothing to change, so DM-002 waits for you with a decision card. Accept it (the round resumes "
         "without a new run), then mark it ready for review.", accept_no_change),
    Flow("merge", "/qa/github",
         "PR links on the task pages point at the pretend GitHub at /qa/github. Merge the PRs of DM-001, DM-002 and DM-003 "
         "there; on the next tick each task is done and its page says the PR merged.", merge),
    Flow("close a phase", "/phases/demo/p1",
         "Cancel DM-004 (it was never started). On the phase page press 'Close phase'; the page shows the phase closed "
         "and /herbarium lists it.", close_a_phase),
]


def run_scripted(base_url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Drive every flow in order with the scripted agent. Stops at the first failure, since
    later flows build on earlier ones. Returns the same shape a real agent reports."""
    c = Client(base_url, timeout=timeout)
    flows: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    try:
        for f in FLOWS:
            try:
                f.run(c)
                flows.append({"name": f.name, "ok": True, "page": f.page, "note": ""})
            except FlowFailed as e:
                flows.append({"name": f.name, "ok": False, "page": c.last_page, "note": str(e)})
                findings.append({"page": c.last_page, "text": f"{f.name}: {e}"})
                break
            except httpx.HTTPError as e:
                flows.append({"name": f.name, "ok": False, "page": c.last_page, "note": f"request failed: {e}"})
                findings.append({"page": c.last_page, "text": f"{f.name}: request failed: {e}"})
                break
    finally:
        c.close()
    done = sum(1 for f in flows if f["ok"])
    return {"flows": flows, "findings": findings, "summary": f"scripted agent: {done} of {len(FLOWS)} flows completed"}
