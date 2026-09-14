"""Standalone, inert HTML explanations derived from a pull request diff.

The report deliberately has no active content: PR source is data, never HTML.  Keeping
generation here also lets a future model-backed narrator use the same provenance and
artifact boundary without changing the task page.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlsplit

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
        source = html.escape(raw)
        source = re.sub(r"\b(def|class|return|if|else|for|from|import|raise|try|except|with)\b",
                        r'<b class="kw">\1</b>', source)
        rows.append(f'<span class="{kind}"><i>{number:>5}</i>{source}</span>')
    if len(lines) > limit:
        rows.append('<span class="context"><i>     </i>… selected patch truncated …</span>')
    return "\n".join(rows) or '<span class="context"><i>     </i>No textual patch was available.</span>'


def _added(lines: list[str]) -> list[str]:
    return [line[1:].strip() for line in lines if line.startswith("+") and not line.startswith("+++")]


def _symbols(lines: list[str]) -> list[str]:
    found = []
    for line in _added(lines):
        match = re.match(r"(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", line)
        if match:
            found.append(match.group(1))
    return found


def _quoted_labels(lines: list[str]) -> list[str]:
    labels = []
    for line in _added(lines):
        labels.extend(value.strip() for value in re.findall(r"[\"']([^\"'\n]{4,60})[\"']", line)
                      if " " in value and not value.startswith(("http", "/")))
    return list(dict.fromkeys(labels))[:3]


def _responsibility(path: str, lines: list[str], source: str) -> str:
    """Describe what the changed component actually declares, with its role as context."""
    symbols = _symbols(lines)
    routes = re.findall(r"@(?:app|router)\.(?:get|post|put|delete)\([\"']([^\"']+)", source)
    labels = _quoted_labels(lines)
    if routes:
        detail = ", ".join(f"`{route}`" for route in routes[:3])
        return f"Handles the HTTP endpoint{'s' if len(routes) != 1 else ''} {detail}."
    if "/templates/" in path or path.endswith((".html", ".jinja", ".jinja2")):
        detail = f" The changed UI includes “{'”, “'.join(labels)}”." if labels else ""
        return f"Renders the user-facing controls and report state for this page.{detail}"
    if path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_"):
        named = f" through `{', '.join(symbols[:3])}`" if symbols else ""
        return f"Defines regression scenarios{named} that exercise the changed contract."
    if symbols:
        return f"Defines the changed application behavior in `{', '.join(symbols[:4])}`."
    return "Carries the source changes shown in the walkthrough; no narrower responsibility is evidenced by the patch."


def _annotation(lines: list[str]) -> str:
    """Explain the operations visible in a selected patch without inventing intent."""
    additions = _added(lines)
    symbols = _symbols(lines)
    calls = []
    for line in additions:
        if line.startswith(("@", "def ", "async def ", "class ")):
            continue
        calls.extend(re.findall(r"\b([A-Za-z_]\w*)\(", line))
    calls = [call for call in dict.fromkeys(calls) if call not in symbols and call not in {"if", "for"}]
    effects = []
    if symbols:
        effects.append(f"introduces the entry point{'s' if len(symbols) != 1 else ''} `{', '.join(symbols[:3])}`")
    if calls:
        effects.append(f"delegates work to `{', '.join(calls[:4])}`")
    if any(re.search(r"\b(return|raise)\b", line) for line in additions):
        effects.append("adds an explicit result or failure path")
    if any("write_text(" in line or "save(" in line for line in additions):
        effects.append("persists its output")
    if any(line.startswith(("@app.", "@router.")) for line in additions):
        effects.append("exposes the behavior as an HTTP route")
    if not effects:
        effects.append("changes the concrete statements highlighted below")
    return ("This hunk " + "; ".join(effects) + ". These claims come from the shown "
            "declarations and calls; runtime behavior is not observed by report generation.")


def _relationships(changed: list[tuple[str, list[str]]], sources: dict[str, str]) -> list[tuple[int, int, str]]:
    """Return only cross-file relationships evidenced by a name/import reference."""
    relationships = []
    for target_index, (target_path, target_lines) in enumerate(changed, 1):
        target_names = set(_symbols(target_lines))
        target_module = Path(target_path).stem
        if not target_names and target_module == "__init__":
            continue
        for source_index, (source_path, _) in enumerate(changed, 1):
            if source_index == target_index:
                continue
            source = sources.get(source_path, "")
            matched = sorted(name for name in target_names if re.search(rf"\b{re.escape(name)}\b", source))
            module_match = re.search(rf"\b(?:from|import)\s+[\w.]*{re.escape(target_module)}\b", source)
            if matched or module_match:
                evidence = f"references `{', '.join(matched[:3])}`" if matched else f"imports `{target_module}`"
                relationships.append((source_index, target_index, evidence))
    return relationships


def _behavior_changes(changed: list[tuple[str, list[str]]]) -> list[str]:
    """Summarize externally legible additions, retaining the source evidence in the text."""
    changes = []
    for path, lines in changed:
        additions = _added(lines)
        joined = "\n".join(additions)
        routes = re.findall(r"@(?:app|router)\.(?:get|post|put|delete)\([\"']([^\"']+)", joined)
        labels = _quoted_labels(lines) if "/templates/" in path or path.endswith(".html") else []
        symbols = _symbols(lines)
        if routes:
            changes.append(f"{path} adds the endpoint{'s' if len(routes) != 1 else ''} "
                           + ", ".join(f"`{route}`" for route in routes[:3]))
        if labels:
            changes.append(f"{path} adds reader-facing text including “{'”, “'.join(labels)}”")
        if symbols and not (path.startswith("tests/") or "/tests/" in path):
            changes.append(f"{path} adds `{', '.join(symbols[:3])}`")
    return changes[:8]


def render(task: Task, *, base: str, head: str, diff: str,
           sources: dict[str, str] | None = None) -> str:
    """Build a readable report from verified source inputs without interpreting markup."""
    changed = _files(diff)
    sources = sources or {}
    pr = html.escape(task.pr or "No remote pull-request URL recorded")
    file_list = "".join(f'<li><a href="#file-{index}"><code>{html.escape(path)}</code></a></li>'
                        for index, (path, _) in enumerate(changed, 1)) or "<li>No changed files found.</li>"
    sections = []
    parsed_pr = urlsplit(task.pr) if task.pr else None
    pr_url = task.pr if parsed_pr and parsed_pr.scheme in {"http", "https"} and parsed_pr.netloc else ""
    for index, (path, lines) in enumerate(changed, 1):
        # GitHub's per-file fragment is an implementation detail and differs by provider;
        # the PR's stable Files view is still the corresponding authoritative code source.
        code_url = f"{pr_url}/files" if pr_url else ""
        link = f' · <a href="{html.escape(code_url, quote=True)}">Open in PR</a>' if code_url else ""
        sections.append(
            f'<section id="file-{index}"><h2><code>{html.escape(path)}</code>{link}</h2>'
            f'<p><strong>Responsibility.</strong> {html.escape(_responsibility(path, lines, sources.get(path, "")))}</p>'
            f'<aside><strong>Why this change matters.</strong> {html.escape(_annotation(lines))}</aside>'
            '<p>Important changed lines and their supplied surrounding context are shown below. '
            'Green lines are additions; red lines are removals.</p>'
            f'<pre><code>{_snippet(lines, 48)}</code></pre></section>'
        )
    purpose = html.escape(task.title)
    count = len(changed)
    relationships = _relationships(changed, sources)
    behaviors = _behavior_changes(changed)
    behavior_html = ("<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in behaviors) + "</ul>"
                     if behaviors else
                     "<p>No externally legible addition could be identified from the textual patch.</p>")
    if relationships:
        flow = "".join(
            f'<li><a href="#file-{source}">{html.escape(Path(changed[source - 1][0]).name)}</a> → '
            f'<a href="#file-{target}">{html.escape(Path(changed[target - 1][0]).name)}</a>: '
            f'{html.escape(evidence)}</li>' for source, target, evidence in relationships
        )
        interaction = ("<p>These connections are derived from cross-file symbol or import references in the "
                       f"checked-out source.</p><ul aria-label=\"component relationships\">{flow}</ul>")
    else:
        interaction = ("<p>No cross-file call or import relationship can be established from the available "
                       "source. The list below is a component inventory, not an execution flow.</p>")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PR explanation · {html.escape(task.id)}</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.55 system-ui,sans-serif;max-width:1000px;margin:3rem auto;padding:0 1.2rem}}nav,aside{{padding:1rem;background:#8882;border-radius:.5rem}}code{{font-family:ui-monospace,monospace}}pre{{padding:1rem;overflow:auto;background:#8882;border-radius:.5rem}}pre code span{{display:block;white-space:pre}}pre i{{display:inline-block;width:4.5rem;color:#777;font-style:normal;user-select:none}}.added{{background:#25833d33}}.removed{{background:#bd303033}}.hunk{{color:#777}}.kw{{color:#9855d4}}a{{color:#2878c8}}h1,h2{{line-height:1.2}}</style></head><body><main>
<h1>Pull request explanation</h1><p><strong>{purpose}</strong></p>
<nav><a href="#overview">Overview</a> · <a href="#components">Components</a> · <a href="#walkthrough">Code walkthrough</a></nav>
<section id="overview"><h2>Overview</h2><p><strong>Purpose.</strong> {purpose}. The patch changes {count} file{'s' if count != 1 else ''}.</p><h3>Observable changes evidenced by additions</h3>{behavior_html}<dl><dt>Pull request</dt><dd>{pr}</dd><dt>Base revision</dt><dd><code>{html.escape(base)}</code></dd><dt>Source revision</dt><dd><code>{html.escape(head)}</code></dd><dt>Generated</dt><dd>{html.escape(now_iso())}</dd></dl><p><em>Intent comes from the task title and explanations are conservative inferences from the source patch, not observed runtime evidence. The report does not approve or merge the PR.</em></p></section>
<section id="components"><h2>Components and interactions</h2>{interaction}<p>Follow each component for its source-grounded responsibility, annotation, and code.</p><ol>{file_list}</ol></section>
<section id="walkthrough"><h2>Code walkthrough</h2>{''.join(sections) or '<p>No patch could be loaded. Regenerate after the branch is available.</p>'}</section>
</main></body></html>'''


def generate(task: Task, *, garden_dir: Path, repo: Path, base: str,
             expected_head: str = "") -> dict[str, str]:
    """Write one immutable report per source head and return its inspectable provenance."""
    head = gitops.head_sha(repo)
    if not head:
        raise RuntimeError("the PR source checkout is unavailable; retry after it is available")
    if expected_head and head != expected_head:
        raise RuntimeError(
            f"the PR source moved while preparing the report; expected {expected_head[:12]}, got {head[:12]}"
        )
    diff = gitops.diff(repo, base)
    if not diff:
        raise RuntimeError("no PR diff is available; retry after the branch and base are fetched")
    path = report_path(garden_dir, task.id, head)
    path.parent.mkdir(parents=True, exist_ok=True)
    sources = {}
    for changed_path, _ in _files(diff):
        candidate = (repo / changed_path).resolve()
        try:
            candidate.relative_to(repo.resolve())
            sources[changed_path] = candidate.read_text(encoding="utf-8")[:200_000]
        except (OSError, UnicodeError, ValueError):
            sources[changed_path] = ""
    path.write_text(render(task, base=base, head=head, diff=diff, sources=sources), encoding="utf-8")
    return {"status": "ready", "path": str(path), "base": base, "head": head, "generated_at": now_iso()}
