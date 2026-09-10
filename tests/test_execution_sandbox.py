from __future__ import annotations

import json
from pathlib import Path

import pytest

from garden.checks import run_check
from garden.harness import Harness
from garden.sandbox import SandboxError, SandboxPolicy


def test_required_claude_policy_reports_filesystem_network_and_inheritance(tmp_path: Path):
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "network_destinations": ["api.example.test:443"],
                    "command": ["/bin/sh"]},
    })
    cmd = Harness("claude", {}).command(worktree=tmp_path, sandbox_policy=policy)
    settings = json.loads(cmd[cmd.index("--settings") + 1])

    assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(tmp_path), "$TMPDIR"]
    assert settings["sandbox"]["filesystem"]["denyWrite"] == ["//"]
    assert settings["sandbox"]["network"]["allowedDomains"] == ["api.example.test:443"]
    assert policy.report_env("configured-os-wrapper+claude-native") == {
        "GARDEN_SANDBOX_MECHANISM": "configured-os-wrapper+claude-native",
        "GARDEN_SANDBOX_ENFORCED": "1",
    }


@pytest.mark.parametrize("name, mode", [
    ("claude", "bypass"),
    ("codex", "dangerously-bypass-approvals-and-sandbox"),
])
def test_required_policy_rejects_harness_bypass(name: str, mode: str, tmp_path: Path):
    with pytest.raises(SandboxError, match="bypass"):
        Harness(name, {"permission_mode": mode}).command(
            worktree=tmp_path,
            sandbox_policy=SandboxPolicy.from_config({
                "sandbox": {"required": True, "command": ["/bin/sh"]},
            }),
        )


def test_required_policy_rejects_missing_check_enforcer(tmp_path: Path):
    result = run_check(
        {"name": "hostile", "command": "touch ../controller"}, {}, cwd=tmp_path,
        config={"sandbox": {"required": True}},
    )
    assert result["status"] == "error"
    assert "sandbox.command" in result["summary"]
    assert not (tmp_path.parent / "controller").exists()


def test_required_policy_rejects_custom_harness(tmp_path: Path):
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "network_destinations": ["example.test"],
                    "command": ["/bin/sh"]},
    })
    cmd = Harness("codex", {}).command(worktree=tmp_path, sandbox_policy=policy)
    assert "sandbox_workspace_write.network_access=false" in cmd
    with pytest.raises(SandboxError, match="does not declare"):
        Harness("custom", {"command": ["agent"]}).command(worktree=tmp_path, sandbox_policy=policy)


def test_policy_rejects_path_shaped_network_destination():
    with pytest.raises(SandboxError, match="host names"):
        SandboxPolicy.from_config({
            "sandbox": {"required": True, "network_destinations": ["https://example.test/all"]},
        })
