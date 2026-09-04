"""Model trials: run one task with several harness/model contenders, compare the resulting
PRs with one comparison run, keep the winner, and record relative scores.

Records go to .garden/trials.jsonl; `garden trials` shows the leaderboard.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .brief import build_brief
from .model import Task, now_iso
from .store import Store

COMPARE_MARKER = "GARDEN_COMPARE:"

COMPARE_RULES = """\
## Your job

Several independent workers implemented the same task, each on its own branch. Compare the
results and rank them. You are in a worktree of contender **{first}**; every contender's
worktree path is listed below so you can run the project's checks in each. Do NOT modify
any file.

Judge each contender on, in order: (1) acceptance criteria met, with evidence; (2)
correctness and robustness; (3) code quality and fit with the existing codebase; (4) scope
discipline (nothing extra, nothing missing); (5) PR description quality. Give each a score
from 0 to 10 (10 = merge as is), independent of the others, then pick the winner. Ties go
to the smaller diff.

End your final message with exactly one line:

  {marker} {{"winner": "<label>", "rationale": "<2-4 sentences>", "ranking": [{{"label": "<label>", "score": <0-10>, "summary": "<one sentence>"}}]}}

The JSON must be on one line and include every contender.
"""


def parse_contender(spec: str, default_harness: str) -> tuple[str, str, str]:
    """'claude:opus' -> (label, harness, model); 'opus' -> default harness."""
    spec = spec.strip()
    if ":" in spec:
        harness, model = spec.split(":", 1)
    else:
        harness, model = default_harness, spec
    harness = harness or default_harness
    label = f"{harness}:{model}" if model else harness
    return label, harness, model


def compare_brief(store: Store, task: Task, contenders: list[dict[str, Any]], diffs: dict[str, str], base: str,
                  max_diff_chars: int) -> str:
    tb = build_brief(store, task, include_rules=False)
    parts = [f"# Trial comparison for task {task.id} ({task.title})\n",
             COMPARE_RULES.format(first=contenders[0]["label"], marker=COMPARE_MARKER),
             "## Task brief (what every worker was given)\n\n" + tb.text,
             "## Contenders\n\n" + "\n".join(f"- **{c['label']}** — branch `{c['branch']}`, worktree `{c['worktree']}`, PR {c.get('pr') or '(none)'}" for c in contenders) + "\n"]
    for c in contenders:
        d = diffs.get(c["label"], "")
        parts.append(f"## PR description: {c['label']}\n\n{c.get('pr_body') or '(empty)'}\n")
        if d and len(d) <= max_diff_chars:
            fence = "````" if "```" in d else "```"
            parts.append(f"## Diff: {c['label']} ({base}...HEAD)\n\n{fence}diff\n{d.rstrip()}\n{fence}\n")
        else:
            parts.append(f"## Diff: {c['label']}\n\nToo large to inline ({len(d):,} chars); run `git -C {c['worktree']} diff {base}...HEAD`.\n")
    return "\n".join(parts)


def parse_compare(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(COMPARE_MARKER):
            payload = line[len(COMPARE_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and data.get("ranking"):
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def ranking_markdown(trial: dict[str, Any]) -> str:
    rows = sorted(trial.get("contenders", []), key=lambda c: -(c.get("score") if isinstance(c.get("score"), (int, float)) else -1))
    out = [f"🏁 **Model trial for {trial.get('task')}** — winner: **{trial.get('winner')}**", "",
           "| contender | score | cost | PR | note |", "|---|---|---|---|---|"]
    for c in rows:
        score = c.get("score")
        cost = f"${c['cost']:.2f}" if c.get("cost") is not None else "–"
        out.append(f"| {c['label']} | {score if score is not None else '–'} | {cost} | {c.get('pr') or '–'} | {c.get('summary') or c.get('status') or ''} |")
    if trial.get("rationale"):
        out += ["", trial["rationale"]]
    return "\n".join(out)


class TrialLog:
    def __init__(self, path: Path):
        self.path = path

    def record(self, trial: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"at": now_iso(), **trial}, sort_keys=True) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def leaderboard(self) -> list[dict[str, Any]]:
        agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"trials": 0, "wins": 0, "scores": [], "costs": [], "failed": 0})
        for t in self.read():
            for c in t.get("contenders", []):
                a = agg[c["label"]]
                a["trials"] += 1
                if c.get("status") == "failed":
                    a["failed"] += 1
                if isinstance(c.get("score"), (int, float)):
                    a["scores"].append(float(c["score"]))
                if isinstance(c.get("cost"), (int, float)):
                    a["costs"].append(float(c["cost"]))
                if t.get("winner") == c["label"]:
                    a["wins"] += 1
        rows = []
        for label, a in agg.items():
            rows.append({
                "label": label, "trials": a["trials"], "wins": a["wins"], "failed": a["failed"],
                "win_rate": round(a["wins"] / a["trials"], 2) if a["trials"] else None,
                "avg_score": round(sum(a["scores"]) / len(a["scores"]), 2) if a["scores"] else None,
                "avg_cost": round(sum(a["costs"]) / len(a["costs"]), 4) if a["costs"] else None,
            })
        rows.sort(key=lambda r: (-(r["avg_score"] or 0), -(r["win_rate"] or 0)))
        return rows
