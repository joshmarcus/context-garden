"""Token-free stand-in for an exact-head CI query command (see docs/worker-ci.md).

Usage: ``fake_ci_command.py <plan.json> <candidate commit>``.  The plan maps a commit to
the answer this fake CI gives for it, with an optional ``default`` for every other commit:

    {"by_sha": {"<sha>": {"answer": {"state": "success", "exists_for_sha": true,
                                     "stale": false}}},
     "default": {"answer": {"state": "missing"}}}

An entry may instead ask the fake to stall (``sleep_seconds``), exit nonzero
(``exit_code``), write to stderr (``stderr``) or print something other than one JSON
object (``raw_stdout``), so a test can cover every fail-closed path without a real CI
system, a network or a credential.  Every query is appended to ``queries.log`` beside the
plan, which is how a test sees how often the scheduler asked.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    plan_path, sha = Path(sys.argv[1]), sys.argv[2]
    plan = json.loads(plan_path.read_text())
    with (plan_path.parent / "queries.log").open("a") as log:
        log.write(f"{sha}\n")
    row = dict(plan.get("by_sha", {}).get(sha) or plan.get("default") or {})
    if row.get("sleep_seconds"):
        time.sleep(float(row["sleep_seconds"]))
    if "raw_stdout" in row:
        sys.stdout.write(str(row["raw_stdout"]))
    elif "answer" in row:
        # The commit is echoed from argv unless the plan overrides it deliberately.
        sys.stdout.write(json.dumps({"sha": sha, **row["answer"]}))
    if row.get("stderr"):
        sys.stderr.write(str(row["stderr"]))
    return int(row.get("exit_code", 0))


if __name__ == "__main__":
    raise SystemExit(main())
