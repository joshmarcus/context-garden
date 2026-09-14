"""Standalone, inert HTML explanations derived from a pull request diff.

The report deliberately has no active content: PR source is data, never HTML.  Keeping
generation here also lets a future model-backed narrator use the same provenance and
artifact boundary without changing the task page.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import gitops
from .model import Task, now_iso


def report_path(garden_dir: Path, task_id: str, head: str) -> Path:
    """A task id and git SHA are controlled identifiers, not a user-supplied path."""
    safe_task = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)
    safe_head = re.sub(r"[^0-9a-f]", "", head.lower())[:40] or "unresolved"
    return garden_dir / "pr-explanations" / safe_task / f"{safe_head}.html"


def _files(diff: str) -> list[tuple[str, list[str]]]:
    files: list[tuple[str, list[str]]] = []
    path = ""
    lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if path:
                files.append((path, lines))
            match = re.match(r"diff --git a/(.*?) b/", line)
            path, lines = (match.group(1) if match else "changed file"), []
        elif path and (line.startswith("@@ ") or line.startswith(("+", "-", " "))):
            lines.append(line)
    if path:
        files.append((path, lines))
    return files


def _snippet(lines: list[str], limit: int = 90) -> str:
    """Render the selected patch as escaped text with visible source line numbers."""
    old = new = 0
    rows = []
    for raw in lines[:limit]:
        if raw.startswith("@@"):
            match = re.search(r"-(\d+).*?\+(\d+)", raw)
            if match:
                old, new = int(match.group(1)), int(match.group(2))
            rows.append(f'<span class="hunk">{html.escape(raw)}</span>')
            continue
        prefix = raw[:1]
        if prefix not in "+- ":
            continue
        number = old if prefix == "-" else new
        if prefix != "+":
            old += 1
        if prefix != "-":
            new += 1
        kind = {"+": "added", "-": "removed", " ": "context"}[prefix]
        rows.append(f'<span class="{kind}"><i>{number:>5}</i>{html.escape(raw)}</span>')
    if len(lines) > limit:
        rows.append('<span class="context"><i>     </i>… selected patch truncated …</span>')
    return "\n".join(rows) or '<span class="context"><i>     </i>No textual patch was available.</span>'


def render(task: Task, *, base: str, head: str, diff: str) -> str:
    """Build a readable report from verified source inputs without interpreting markup."""
    changed = _files(diff)
    pr = html.escape(task.pr or "No remote pull-request URL recorded")
    file_list = "".join(f'<li><a href="#file-{index}"><code>{html.escape(path)}</code></a></li>'
                        for index, (path, _) in enumerate(changed, 1)) or "<li>No changed files found.</li>"
    sections = []
    parsed_pr = urlsplit(task.pr) if task.pr else None
    pr_url = task.pr if parsed_pr and parsed_pr.scheme in {"http", "https"} and parsed_pr.netloc else ""
    for index, (path, lines) in enumerate(changed, 1):
        code_url = f"{pr_url}/files#file-{quote(path, safe='')}" if pr_url else ""
        link = f' · <a href="{html.escape(code_url, quote=True)}">Open in PR</a>' if code_url else ""
        sections.append(
            f'<section id="file-{index}"><h2><code>{html.escape(path)}</code>{link}</h2>'
            '<p>This patch is shown with surrounding unchanged lines when the diff supplied them. '
            'Green lines are additions; red lines are removals.</p>'
            f'<pre><code>{_snippet(lines)}</code></pre></section>'
        )
    purpose = html.escape(task.title)
    count = len(changed)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PR explanation · {html.escape(task.id)}</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.55 system-ui,sans-serif;max-width:1000px;margin:3rem auto;padding:0 1.2rem}}nav{{padding:1rem;background:#8882;border-radius:.5rem}}code{{font-family:ui-monospace,monospace}}pre{{padding:1rem;overflow:auto;background:#8882;border-radius:.5rem}}pre code span{{display:block;white-space:pre}}pre i{{display:inline-block;width:4.5rem;color:#777;font-style:normal;user-select:none}}.added{{background:#25833d33}}.removed{{background:#bd303033}}.hunk{{color:#777}}a{{color:#2878c8}}h1,h2{{line-height:1.2}}</style></head><body><main>
<h1>Pull request explanation</h1><p><strong>{purpose}</strong></p>
<nav><a href="#overview">Overview</a> · <a href="#components">Components</a> · <a href="#walkthrough">Code walkthrough</a></nav>
<section id="overview"><h2>Overview</h2><p>This report is grounded in the PR patch rather than a merge or approval decision. It changes {count} file{'s' if count != 1 else ''}. The task title describes the intended purpose; observable behavior should be confirmed from the patch below.</p><dl><dt>Pull request</dt><dd>{pr}</dd><dt>Base revision</dt><dd><code>{html.escape(base)}</code></dd><dt>Source revision</dt><dd><code>{html.escape(head)}</code></dd><dt>Generated</dt><dd>{html.escape(now_iso())}</dd></dl><p><em>No model-derived intent is asserted in this report. If the task title and patch disagree, the patch is the available evidence.</em></p></section>
<section id="components"><h2>Components and flow</h2><p>Each changed file is a component affected by this PR. Read the patch in file order; where the change crosses files, the changed symbols and imports in the snippets show the handoff.</p><ol>{file_list}</ol></section>
<section id="walkthrough"><h2>Code walkthrough</h2>{''.join(sections) or '<p>No patch could be loaded. Regenerate after the branch is available.</p>'}</section>
</main></body></html>'''


def generate(task: Task, *, garden_dir: Path, repo: Path, base: str) -> dict[str, str]:
    """Write one immutable report per source head and return its inspectable provenance."""
    head = gitops.head_sha(repo)
    if not head:
        raise RuntimeError("the PR source checkout is unavailable; retry after it is available")
    diff = gitops.diff(repo, base)
    if not diff:
        raise RuntimeError("no PR diff is available; retry after the branch and base are fetched")
    path = report_path(garden_dir, task.id, head)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(task, base=base, head=head, diff=diff), encoding="utf-8")
    return {"status": "ready", "path": str(path), "base": base, "head": head, "generated_at": now_iso()}
