"""Durable deep-dive reports and isolated publication to the workspace repository."""

from __future__ import annotations

import html
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import markdown as md

from .web.trust import sanitize_html

_SECRET_PATTERNS = (
    re.compile(r"(?im)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\b\s*[:=]\s*)\S+"),
    re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(https?://[^\s:/]+:)[^\s/@]+(@)"),
    re.compile(r"(?s)-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----"),
)


def redact_secrets(text: str) -> str:
    """Remove common credential forms before either durable report is written."""
    value = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            value = pattern.sub(r"\1[REDACTED]\2", value)
        elif pattern.groups == 1:
            value = pattern.sub(r"\1[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value


def report_markdown(identity: str, question: str, report: dict[str, Any]) -> str:
    """Render the structured result once; HTML is derived from this same source."""
    def section(title: str, value: Any) -> str:
        if isinstance(value, list):
            rows = []
            for item in value:
                text = str(item)
                if title == "Related work" and (text.startswith(("https://", "http://", "/"))):
                    text = f"[{text}]({text})"
                rows.append(f"- {text}")
            body = "\n".join(rows) or "- None recorded."
        else:
            body = str(value or "Not established.")
        return f"## {title}\n\n{body}"

    return "\n\n".join([
        f"# Deep dive {identity}", section("Question", question),
        section("Timeline and evidence", report.get("evidence")),
        section("Source and run identities", report.get("source_identities")),
        section("Observed behavior", report.get("observed_behavior")),
        section("Intended behavior", report.get("intended_behavior")),
        section("Root cause", report.get("likely_cause")),
        section("Confidence and remaining uncertainty",
                [f"Confidence: {report.get('confidence', 'unknown')}", *report.get("unknowns", [])]),
        section("Impact", report.get("impact")),
        section("Checks performed", report.get("attempted_checks")),
        section("Corrective action", report.get("corrective_action") or report.get("recommendation")),
        section("Related work", report.get("links")),
    ]) + "\n"


def report_html(markdown: str, identity: str) -> str:
    """Return a self-contained, sanitized document with useful Markdown links."""
    safe = sanitize_html(md.markdown(markdown, extensions=["fenced_code", "tables", "sane_lists"]))
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Deep dive {html.escape(identity)}</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.55 system-ui,sans-serif;max-width:900px;margin:3rem auto;padding:0 1.2rem}}pre,code{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#2878c8}}h1,h2{{line-height:1.2}}li{{margin:.3rem 0}}</style></head>
<body><main>{safe}</main></body></html>"""


def save_report(run_path: Path, identity: str, question: str,
                report: dict[str, Any]) -> tuple[Path, Path]:
    markdown = redact_secrets(report_markdown(identity, question, report))
    md_path, html_path = run_path / "report.md", run_path / "report.html"
    md_path.write_text(markdown)
    html_path.write_text(report_html(markdown, identity))
    return md_path, html_path


def publish_report(workspace: Path, identity: str, md_path: Path, html_path: Path) -> dict[str, str]:
    """Publish without touching the operator checkout or overwriting another publisher."""
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=workspace,
                            text=True, capture_output=True, check=True).stdout.strip()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=workspace,
                            text=True, capture_output=True, check=True).stdout.strip()
    if not remote or not branch:
        raise RuntimeError("workspace origin and branch must be configured")
    relative = Path("reports") / "investigations"
    with tempfile.TemporaryDirectory(prefix="garden-deep-dive-") as raw:
        clone = Path(raw) / "workspace"
        subprocess.run(["git", "clone", "--quiet", "--branch", branch, "--single-branch", remote, str(clone)], check=True)
        target = clone / relative
        target.mkdir(parents=True, exist_ok=True)
        published_md, published_html = target / f"{identity}.md", target / f"{identity}.html"
        published_md.write_bytes(md_path.read_bytes())
        published_html.write_bytes(html_path.read_bytes())
        subprocess.run(["git", "add", str(published_md), str(published_html)], cwd=clone, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=clone).returncode != 0
        if changed:
            subprocess.run(["git", "-c", "user.name=Garden", "-c", "user.email=garden@localhost",
                            "commit", "--quiet", "-m", f"Publish deep dive {identity}"], cwd=clone, check=True)
            subprocess.run(["git", "push", "--quiet", "origin", f"HEAD:{branch}"], cwd=clone, check=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, text=True,
                                capture_output=True, check=True).stdout.strip()
    return {"remote": remote, "branch": branch, "commit": commit,
            "markdown": (relative / published_md.name).as_posix(),
            "html": (relative / published_html.name).as_posix()}
