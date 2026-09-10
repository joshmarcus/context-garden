"""Queue runs for workers that pull them from the garden web API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..runs import Run
from .base import Runner, RunnerError, pass_env_patterns


class RemoteRunner(Runner):
    name = "remote"
    remote = True

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if bool((self.config.get("sandbox") or {}).get("required")):
            raise RunnerError(
                "sandbox.required needs a remote worker capability attestation; "
                "this remote protocol version does not provide one"
            )
        if self.harness is None and run.mode != "check":
            raise RunnerError("remote runner needs a harness")
        (run.path / "brief.md").write_text(brief_text)
        run.harness = self.harness.name if self.harness else run.harness
        run.status = "running"
        run.save()

    def start_checks(self, run: Run, worktree: Path, payload: dict[str, Any]) -> None:
        if bool((self.config.get("sandbox") or {}).get("required")):
            raise RunnerError(
                "sandbox.required needs a remote worker capability attestation; "
                "this remote protocol version does not provide one"
            )
        (run.path / "checks_input.json").write_text(json.dumps(payload))
        run.status = "running"
        run.save()

    def collect(self, run: Run) -> dict[str, Any]:
        posted = run.path / "remote_result.json"
        if posted.exists():
            return json.loads(posted.read_text())
        if self.harness is None:
            return {"result": {}, "usage": {}, "cost_usd": None, "final_text": "", "error": "missing remote result"}
        return self.harness.parse(run.stdout_text(), run.stderr_text(), run.path / "final.md", model=run.model)

    def doctor(self) -> list[str]:
        from ..hosts.registry import enrolled_hosts

        hosts = list(self.config.get("hosts") or [])
        try:
            enrolled = enrolled_hosts(self.config)
        except (OSError, TypeError, ValueError) as exc:
            return [f"remote runner: {exc}"]
        if not hosts and not enrolled:
            return ["remote runner: no hosts configured under workers.hosts"]
        return [f"remote worker {h.get('name', '?')}: token_env is required"
                for h in hosts if not str(h.get("token_env") or "")]

    def claim_env_names(self) -> list[str]:
        return pass_env_patterns(self.config)
