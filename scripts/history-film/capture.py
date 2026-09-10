"""Capture Context Garden's development history for the offline film renderer."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import yaml


def timestamp(value: object) -> int:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=120)


def read_events(file: Path) -> tuple[list[dict], str]:
    raw = file.read_bytes()
    events = []
    for line, value in enumerate(raw.splitlines(), 1):
        if not value.strip():
            continue
        try:
            event = json.loads(value)
            event["ms"] = timestamp(event["at"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid event at line {line}; capture a complete event log") from exc
        events.append(event)
    if not events:
        raise ValueError("The event log is empty")
    return sorted(events, key=lambda e: e["ms"]), hashlib.sha256(raw).hexdigest()


def read_tasks(product: Path) -> tuple[list[dict], str]:
    tasks = []
    digest = hashlib.sha256()
    for file in sorted(product.rglob("CG-*.md")):
        if any(p.startswith(".") for p in file.relative_to(product).parts):
            continue
        raw = file.read_bytes()
        digest.update(file.relative_to(product).as_posix().encode() + b"\0" + raw + b"\0")
        parts = raw.decode("utf-8-sig").split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            continue
        metadata = yaml.safe_load(parts[1])
        if isinstance(metadata, dict) and re.fullmatch(r"CG-\d+", str(metadata.get("id", ""))):
            tasks.append(metadata)
    if not tasks:
        raise ValueError("No CG task frontmatter found below the product directory")
    return tasks, digest.hexdigest()


def commits_at(repo: Path, ref: str, until: int) -> tuple[str, list[dict], list[str]]:
    head = (
        git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    )
    log = git(repo, "log", head, "--first-parent", "--reverse", "--format=%H%x09%ct%x09%s").decode()
    commits = []
    previous = None
    for line in log.splitlines():
        sha, seconds, subject = line.split("\t", 2)
        raw_at = int(seconds) * 1000
        # A capture is a contiguous first-parent prefix, even with a clock-skewed commit.
        at = max(raw_at, commits[-1]["at"] if commits else raw_at)
        if at > until:
            break
        args = [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--no-renames",
            "-r",
            "-z",
            "--name-status",
        ]
        fields = (
            git(repo, *args, *([previous, sha] if previous else [sha]))
            .decode()
            .rstrip("\0")
            .split("\0")
        )
        changes = (
            [] if fields == [""] else [[fields[i + 1], fields[i]] for i in range(0, len(fields), 2)]
        )
        pr = re.search(r"(?:pull request #|\(#)(\d+)", subject)
        commits.append(
            {
                "sha": sha,
                "at": at,
                "rawAt": raw_at,
                "pr": int(pr[1]) if pr else None,
                "changes": changes,
            }
        )
        previous = sha
    if not commits:
        raise ValueError("No commits within the event interval")
    files = sorted({file for c in commits for file, _ in c["changes"]})
    indices = {file: i for i, file in enumerate(files)}
    for commit in commits:
        commit["changes"] = [[indices[file], op] for file, op in commit["changes"]]
    return commits[-1]["sha"], commits, files


def task_timeline(events: list[dict]) -> list[list]:
    stage = 0
    result = []
    modes = {
        "work": (1, "Build"),
        "resume": (1, "Resume"),
        "revise": (4, "Revise"),
        "rebase": (4, "Rebase"),
        "review": (3, "Review"),
        "check": (2, "Check"),
    }
    statuses = {
        "ready": (0, "Ready"),
        "in_review": (3, "Awaiting review"),
        "changes_requested": (4, "Changes requested"),
        "blocked": (6, "blocked"),
        "failed": (6, "failed"),
        "cancelled": (8, "Cancelled"),
        "done": (8, "Completed"),
    }
    for e in events:
        kind, label, detail = e.get("kind"), "", False
        if kind == "dispatch" and e.get("mode") in modes:
            stage, label = modes[e["mode"]]
            label += " dispatched"
        elif kind == "review" and e.get("verdict") in {"approve", "request_changes"}:
            stage, label = (
                (5, "Review approved") if e["verdict"] == "approve" else (4, "Changes requested")
            )
            detail = bool(e.get("summary"))
        elif kind == "check":
            stage, label = 2, "Check: " + str(e.get("status", "unknown"))
        elif kind == "pr_opened":
            stage, label = 3, "Pull request opened"
        elif kind == "conflict":
            stage, label = 4, "Conflict"
        elif kind == "transition" and e.get("to") in statuses:
            stage, label = statuses[e["to"]]
        if label:
            result.append([e["ms"], stage, label, detail])
    return result


def capture(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise ValueError("Output already exists; choose a new snapshot directory")
    events, event_hash = read_events(args.garden / ".garden" / "events.jsonl")
    tasks, task_hash = read_tasks(args.garden / "context-garden")
    end = timestamp(args.until) if args.until else events[-1]["ms"]
    if end > events[-1]["ms"]:
        raise ValueError("--until extends beyond the available event log")
    events = [e for e in events if e["ms"] <= end]
    if not events:
        raise ValueError("No events before --until")
    head, commits, files = commits_at(args.repo, args.ref, end)
    native = defaultdict(list)
    for e in events:
        native[e.get("task")].append(e)
    by_pr = defaultdict(list)
    for c in commits:
        if c["pr"]:
            by_pr[c["pr"]].append(c)
    rendered = []
    for t in sorted(tasks, key=lambda t: int(t["id"].split("-")[1])):
        timeline = task_timeline(native[t["id"]])
        if not timeline:
            continue
        urls = [str(t.get("pr", ""))]
        for e in native[t["id"]]:
            urls.extend([str(e.get("pr", "")), str(e.get("note", ""))])
        prs = {
            int(m)
            for value in urls
            for m in re.findall(
                r"https://github\.com/joshmarcus/context-garden/pull/(\d+)\b", value
            )
        }
        merges = [c for pr in sorted(prs) for c in by_pr[pr]]
        for c in merges:
            if c.get("task") and c["task"] != t["id"]:
                raise ValueError(f"Ambiguous task ownership for PR {c['pr']}")
            c["task"] = t["id"]
            timeline.append([c["at"], 7, "Merged", True])
        if merges:
            last = max(c["at"] for c in merges)
            timeline = [e for e in timeline if e[0] <= last or e[1] == 7]
        rendered.append(
            {
                "id": t["id"],
                "title": str(t.get("title", t["id"])),
                "phase": str(t.get("phase", "")),
                "created": timestamp(t["created"]) if t.get("created") else timeline[0][0],
                "events": sorted(timeline, key=lambda e: e[0]),
                "merges": [c["sha"] for c in sorted(merges, key=lambda c: c["at"])],
            }
        )
    start = min(commits[0]["at"], events[0]["ms"])
    film = {
        "start": start,
        "end": end,
        "sourceHead": head,
        "tasks": rendered,
        "commits": commits,
        "files": files,
        "stats": {
            "nativeEvents": len(events),
            "gitCommits": len(commits),
            "matchedMerges": sum(bool(c.get("task")) for c in commits),
        },
    }
    phases = {"moves": [], "closed": []}
    for e in events:
        if e.get("kind") == "phase_closed":
            phases["closed"].append({"at": e["ms"], "phase": e["phase"]})
        elif e.get("kind") == "moved":
            phases["moves"].append(
                {"at": e["ms"], "id": e["task"], "from": e["from"], "to": e["to"]}
            )
    dates = [datetime.fromtimestamp(v / 1000, UTC).strftime("%d %b %Y") for v in (start, end)]
    story = {
        "knots": [[0, start], [2, start], [57, end], [60, end]],
        "shots": [],
        "intro": "Real history.",
        "dateLabel": " – ".join(dates),
    }
    args.output.mkdir(parents=True)
    for name, value in (("film.json", film), ("phases.json", phases), ("story.json", story)):
        (args.output / name).write_text(
            json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8"
        )
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("dependencies.py")),
            "--repo",
            str(args.repo),
            "--data",
            str(args.output / "film.json"),
            "--output",
            str(args.output / "dependencies.json"),
        ],
        check=True,
        timeout=600,
    )
    manifest = {
        "schema": 1,
        "sourceHead": head,
        "eventsSha256": event_hash,
        "taskFilesSha256": task_hash,
        "description": "Task titles/metadata are from the supplied capture; their earlier wording is not reconstructed. Clock-skewed commit times are clamped in first-parent order; rawAt retains the original timestamp.",
        "sha256": {
            name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
            for name in ("film.json", "dependencies.json", "phases.json", "story.json")
        },
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Captured {len(rendered)} tasks and {len(commits)} commits in {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--garden",
        type=Path,
        required=True,
        help="Context checkout containing .garden/events.jsonl",
    )
    parser.add_argument(
        "--repo", type=Path, required=True, help="Product Git repository with full history"
    )
    parser.add_argument("--ref", default="HEAD", help="Product history tip; no fetch is performed")
    parser.add_argument("--until", help="UTC ISO timestamp, default last event")
    parser.add_argument("--output", type=Path, required=True, help="New snapshot directory")
    args = parser.parse_args()
    try:
        capture(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Capture failed: {exc}\n")


if __name__ == "__main__":
    main()
