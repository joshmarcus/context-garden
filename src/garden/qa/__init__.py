"""`garden qa`: an agent drives the loop end to end through the web app on a throwaway garden.

`run_qa` builds the throwaway garden (sandbox.py), serves it, hands an agent the flows
(flows.py) as its script, and reads back what the agent completed and what confused it.
The agent is either the built-in scripted one (no tokens; the flows as code) or a real
harness run (`claude -p` with a brief and `curl`). Findings become friction reports on a
phase of the driving garden, each with the page it was seen on and that page's HTML kept in
the run directory; a flow that could not be completed makes the command exit non-zero.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..brief import parse_result
from ..config import no_live_garden_root
from ..harness import Harness
from .flows import FLOWS, run_scripted
from .sandbox import Sandbox, start

QA_MARKER = "GARDEN_QA:"

BRIEF = """\
# QA run: drive the garden's web app end to end

You are the person at the keyboard of a context garden, checking that its web UI lets you
run the whole loop. The garden is a throwaway one: its workers are fakes that finish in a
second and cost nothing, and its GitHub is a pretend one served by the same app. Nothing you
do here reaches a real repository or a real service.

The app is at {base_url}. Use it only over HTTP (`curl` is fine): fetch pages, read them,
post the forms the way a browser would. Buttons are `<form method="post">` elements; post
their action URL with the form fields (`-d note=...`). An action that could not be done
redirects with `?flash=<message>` on the URL: that message is what the person would see.
`GET /api/tasks` returns every task as JSON (`id`, `status`, `pr`) when you want to check a
status without reading a page; the rail's `↻ Tick now` button (`POST /tick`) runs one
scheduler pass right away instead of waiting for the loop's next tick (every second).

Do not run `garden` commands, and do not use anything but the HTTP surface: the point is
to find out whether a person can do this from the pages.

## The flows

Complete these in order; each one builds on the last. The pages named are where a person
would start.

{flows}

## What to report

Report anything that broke, surprised you or made you hesitate: a button you expected and
did not find, a page that did not say what state something was in, a message that did not
explain itself, a step that needed a `tick` you would not have known to press. Each finding
names the page you were on when you noticed it. Be specific and short; the page's HTML is
kept alongside your report.

End your final message with exactly one line of the form:

  GARDEN_QA: {{"flows": [{{"name": "<flow name as given above>", "ok": true|false, "page": "</path you were on>", "note": "<what happened, one line>"}}, ...], "findings": [{{"page": "</path>", "text": "<what broke or confused you>"}}, ...], "summary": "<1-2 sentences>"}}

One entry per flow, in order, with the exact names above. The JSON must be on a single line.
"""


def qa_brief(base_url: str) -> str:
    flows = "\n".join(f"{i}. **{f.name}** (start at `{f.page}`): {f.script}" for i, f in enumerate(FLOWS, 1))
    return BRIEF.format(base_url=base_url, flows=flows)


@dataclass
class QAReport:
    """What a run found: the agent's result, one row per flow, the findings and where the
    pages went."""

    out: Path
    result: dict[str, Any]
    flows: list[dict[str, Any]]
    findings: list[dict[str, str]]
    garden: Path | None = None
    filed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.get("ok") for f in self.flows) and len(self.flows) == len(FLOWS)

    @property
    def failed(self) -> dict[str, Any] | None:
        return next((f for f in self.flows if not f.get("ok")), None)

    def summary(self) -> str:
        lines = []
        for f in self.flows:
            mark = "ok  " if f.get("ok") else "FAIL"
            note = f" · {f['note']}" if f.get("note") else ""
            lines.append(f"  {mark} {f['name']}  ({f.get('page', '')}){note}")
        if self.ok:
            head = f"garden qa: every flow completed ({len(self.flows)} of {len(FLOWS)})"
        elif self.failed is not None:
            head = f"garden qa: FAILED at '{self.failed['name']}' ({self.failed.get('page', '')}): {self.failed.get('note', '')}"
        else:
            missing = [f.name for f in FLOWS if f.name not in {x.get('name') for x in self.flows}]
            head = f"garden qa: FAILED, the agent did not report on: {', '.join(missing)}"
        out = [head, *lines]
        if self.findings:
            out.append(f"  {len(self.findings)} finding(s):")
            for fd in self.findings:
                out.append(f"    - [{fd.get('page', '')}] {fd.get('text', '')}" + (f"  (page: {fd['html']})" if fd.get("html") else ""))
        if self.filed:
            out.append("  filed as friction reports: " + ", ".join(self.filed))
        out.append(f"  run directory: {self.out}")
        return "\n".join(out)


def _normalise(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The agent's result, tidied: flows in the order of the table (an unreported flow is a
    failure), findings as {page, text}."""
    reported = {str(f.get("name", "")).strip().lower(): f for f in result.get("flows") or [] if isinstance(f, dict)}
    flows: list[dict[str, Any]] = []
    for f in FLOWS:
        r = reported.get(f.name.lower())
        if r is None:
            flows.append({"name": f.name, "ok": False, "page": f.page, "note": "not reported by the agent"})
        else:
            flows.append({"name": f.name, "ok": bool(r.get("ok")), "page": str(r.get("page") or f.page), "note": str(r.get("note") or "")})
    findings: list[dict[str, str]] = []
    for fd in result.get("findings") or []:
        if isinstance(fd, dict) and str(fd.get("text") or "").strip():
            findings.append({"page": str(fd.get("page") or ""), "text": str(fd["text"]).strip()})
        elif isinstance(fd, str) and fd.strip():
            findings.append({"page": "", "text": fd.strip()})
    return flows, findings


def run_agent(harness: Harness, model: str, difficulty: str, brief: str, cwd: Path, timeout_minutes: int) -> dict[str, Any]:
    """One headless harness run with the QA brief on stdin; the agent's result JSON, or a
    result with no flows when it produced none (the summary then says why)."""
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "brief.md").write_text(brief)
    cmd = harness.command(model, None, difficulty)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["GARDEN_ROOT"] = no_live_garden_root(cwd)
    started = time.time()
    try:
        proc = subprocess.run(cmd, input=brief, capture_output=True, text=True, env=env, cwd=str(cwd),
                              timeout=timeout_minutes * 60, check=False)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        code = -1
        stderr += f"\nthe agent did not finish within {timeout_minutes} minutes"
    (cwd / "stdout.json").write_text(stdout)
    (cwd / "stderr.log").write_text(stderr)
    parsed = harness.parse(stdout, stderr, None)
    final = parsed.get("final_text") or ""
    (cwd / "final.md").write_text(final)
    result = parse_result(final, QA_MARKER) or parse_result(stdout, QA_MARKER)
    meta = {"exit_code": code, "minutes": round((time.time() - started) / 60, 1), "cost_usd": parsed.get("cost_usd"),
            "usage": parsed.get("usage") or {}, "error": parsed.get("error") or ""}
    (cwd / "run.json").write_text(json.dumps(meta, indent=1))
    if not result:
        why = parsed.get("error") or (f"exit code {code}" if code else "no GARDEN_QA line in the agent's final message")
        return {"flows": [], "findings": [], "summary": f"the agent produced no result ({why})", "_meta": meta}
    result["_meta"] = meta
    return result


def attach_pages(box: Sandbox, findings: list[dict[str, str]]) -> None:
    """Give each finding the recorded HTML of the page it names: the last time the app
    served that path, else a fresh fetch now."""
    import httpx

    for fd in findings:
        page = fd.get("page") or ""
        if not page.startswith("/"):
            continue
        name = box.pages.file_for(page)
        if not name:
            try:
                r = httpx.get(box.base_url + page, timeout=10)
                if r.headers.get("content-type", "").startswith("text/html"):
                    name = box.pages.record(page, r.content)
            except httpx.HTTPError:
                name = ""
        if name:
            fd["html"] = str(box.pages.out / name)


def file_findings(store: Any, product: str, phase: str, findings: list[dict[str, str]], draft_tasks: bool = True) -> list[str]:
    """Each finding becomes a friction report on `product/phase` of the driving garden, with
    the page it was seen on as its provenance and the kept HTML named in the text; a draft
    task too unless `draft_tasks` is off. Returns one label per report filed."""
    from ..friction import append_friction_report, create_friction_draft_task

    ph = store.phase(product, phase)
    doc = ph.path / "docs" / "friction.md"
    date = _dt.date.today().isoformat()
    filed: list[str] = []
    for fd in findings:
        page = fd.get("page") or "web"
        provenance = f"garden qa · {page}"
        text = fd["text"]
        if fd.get("html"):
            text += f"\n\nPage as seen: `{fd['html']}`"
        append_friction_report(doc, text, provenance, date)
        label = page
        if draft_tasks:
            t = create_friction_draft_task(store, product, phase, fd["text"], provenance, date)
            if t is not None:
                label = f"{page} -> {t.id}"
        filed.append(label)
    store.invalidate()
    return filed


def run_qa(
    out: Path,
    *,
    scripted: bool = False,
    harness: Harness | None = None,
    model: str = "",
    difficulty: str = "medium",
    timeout_minutes: int = 30,
    keep: bool = False,
    port: int = 0,
    log: Any = None,
) -> QAReport:
    """Build and serve the throwaway garden, run the agent, gather the report. `out` is the
    run directory (created); the throwaway garden under it is removed unless `keep`."""
    say = log or (lambda m: None)
    out.mkdir(parents=True, exist_ok=True)
    box = start(out, port=port)
    say(f"throwaway garden at {box.garden}, served at {box.base_url}")
    try:
        if scripted:
            say("driving the flows with the scripted agent")
            result = run_scripted(box.base_url)
        else:
            h = harness or Harness("claude", {})
            m = model or h.model_for(difficulty)
            say(f"handing the brief to {h.name}{' ' + m if m else ''}; this costs tokens")
            result = run_agent(h, m, difficulty, qa_brief(box.base_url), out / "agent", timeout_minutes)
        flows, findings = _normalise(result)
        attach_pages(box, findings)
        # the last look at every page a finding names, and at the flow that failed, is on disk
        box.dump_log(out / "tick-log.json")
    finally:
        box.stop()
    (out / "result.json").write_text(json.dumps(result, indent=1))
    (out / "findings.json").write_text(json.dumps(findings, indent=1))
    garden: Path | None = box.garden
    if not keep:
        shutil.rmtree(box.root / "garden", ignore_errors=True)
        shutil.rmtree(box.root / "repo", ignore_errors=True)
        shutil.rmtree(box.root / "remote.git", ignore_errors=True)
        garden = None
    return QAReport(out=out, result=result, flows=flows, findings=findings, garden=garden)
