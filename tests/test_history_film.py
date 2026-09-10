"""The optional film capture reads history without executing product code."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "history-film" / "capture.py"
spec = importlib.util.spec_from_file_location("film_capture", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def test_capture_history_and_dependencies(tmp_path):
    repo, garden, output = (tmp_path / n for n in ("product repo", "context", "snapshot"))
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }

    def git(*args, date="2026-01-01T00:00:00Z"):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            env={**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        )

    git("init")
    code = repo / "src" / "garden"
    code.mkdir(parents=True)
    (code / "a.py").write_text("from . import b\n", encoding="utf-8")
    (code / "b.py").write_text("size = 1\n", encoding="utf-8")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "initial (#12)")
    first = git("rev-parse", "HEAD").decode().strip()
    (code / "b.py").write_text("size = 'more bytes'\n", encoding="utf-8")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "grow", date="2026-01-02T00:00:00Z")
    (code / "b.py").unlink()
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "remove", date="2026-01-03T00:00:00Z")
    (garden / ".garden").mkdir(parents=True)
    tasks = garden / "context-garden" / "phase-01" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "CG-001-example.md").write_text(
        """---
id: CG-001
title: >-
  A multiline
  task title
phase: context-garden/phase-02
pr: https://github.com/joshmarcus/context-garden/pull/12
---
Secret task body must not be exported.
""",
        encoding="utf-8",
    )
    events = [
        {"at": "2025-12-31T20:00:00Z", "kind": "dispatch", "mode": "work", "task": "CG-001"},
        {
            "at": "2025-12-31T21:00:00Z",
            "kind": "review",
            "verdict": "request_changes",
            "summary": "Private feedback",
            "task": "CG-001",
        },
        {"at": "2025-12-31T22:00:00Z", "kind": "review", "verdict": "approve", "task": "CG-001"},
        {
            "at": "2026-01-02T00:00:00Z",
            "kind": "moved",
            "task": "CG-001",
            "from": "context-garden/phase-01",
            "to": "context-garden/phase-02",
        },
        {"at": "2026-01-04T00:00:00Z", "kind": "phase_closed", "phase": "context-garden/phase-01"},
    ]
    log = garden / ".garden" / "events.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    before = log.read_bytes()
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--garden",
            str(garden),
            "--repo",
            str(repo),
            "--output",
            str(output),
        ],
        check=True,
        timeout=30,
    )
    film = json.loads((output / "film.json").read_text())
    dependencies = json.loads((output / "dependencies.json").read_text())
    phases = json.loads((output / "phases.json").read_text())
    assert film["tasks"][0]["title"] == "A multiline task title"
    assert film["tasks"][0]["merges"] == [first]
    assert film["tasks"][0]["events"][1][1:] == [4, "Changes requested", True]
    a, b = [film["files"].index(f"src/garden/{p}.py") for p in ("a", "b")]
    assert [a, b, "import"] in dependencies["edges"]
    sizes = [dict(v["sizes"])[b] for v in dependencies["versions"]]
    assert 0 < sizes[0] < sizes[1]
    assert sizes[2] == 0
    assert dependencies["stats"]["finalEdges"] == 0
    assert phases["moves"][0]["from"] == "context-garden/phase-01"
    assert len(phases["closed"]) == 1
    assert log.read_bytes() == before
    exported = "".join(p.read_text() for p in output.glob("*.json"))
    assert "Secret task body" not in exported and "Private feedback" not in exported


def test_incomplete_event_log_is_not_silently_skipped(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text('{"at": "2026-01-01T00:00:00Z"}\n{"partial":', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        capture.read_events(log)
