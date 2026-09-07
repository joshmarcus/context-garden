"""Scheduler-owned disposable application replay used as review evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .qa import run_qa


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    started = datetime.now(UTC).isoformat()
    report = run_qa(args.out, scripted=True, keep=False)
    finished = datetime.now(UTC).isoformat()
    by_name = {flow["name"]: flow for flow in report.flows}
    state_sources = {
        "affected": ("approve", "approved a draft and observed it become ready"),
        "failure": ("send back with a note", "observed changes requested and a revise action"),
        "recovery": ("send back with a note", "dispatched revision and observed return to triage"),
        "empty": ("close a phase", "completed the journey and observed Inbox zero"),
    }
    states = {
        state: {"status": "pass" if by_name.get(name, {}).get("ok") else "fail",
                "action": name, "observed": observed}
        for state, (name, observed) in state_sources.items()
    }
    events = [
        {"state": state, "action": evidence["action"], "observed": evidence["observed"],
         "at": by_name.get(evidence["action"], {}).get("requests", [{}])[0].get("at")}
        for state, evidence in states.items()
    ]
    manifest = {
        "producer": "garden.scheduler.interaction-replay/v1",
        "head": args.head,
        "nonce": args.nonce,
        "started_at": started,
        "finished_at": finished,
        "status": "pass" if report.ok else "fail",
        "environment": "disposable",
        "flows": report.flows,
        "states": states,
        "events": events,
        "artifacts": [str(args.out / "result.json"), str(args.out / "tick-log.json"),
                      str(args.out / "pages")],
    }
    (args.out / "interaction-manifest.json").write_text(json.dumps(manifest, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
