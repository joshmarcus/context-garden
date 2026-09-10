from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from garden.checks import run_check
from garden.harness import Harness
from garden.sandbox import SandboxError, SandboxPolicy


@pytest.fixture
def sandbox_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wrapper = tmp_path / "sandbox-wrapper"
    wrapper.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys
if sys.argv[1:] == [\"--garden-sandbox-capabilities\"]:
    print(json.dumps({\"contract_version\": 1, \"mechanism\": \"test-sandbox\", \"capabilities\": [
        \"filesystem.readable-roots\", \"filesystem.writable-roots\",
        \"filesystem.protected-roots\", \"filesystem.resolve-symlinks\",
        \"network.destination-allowlist\", \"process.descendants\"]}))
    raise SystemExit(0)
if sys.argv[1] != \"--garden-sandbox-policy\": raise SystemExit(2)
policy = json.loads(sys.argv[2])
raise SystemExit(subprocess.run(sys.argv[sys.argv.index(\"--\") + 1:], env=os.environ).returncode)
""")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(SandboxPolicy, "_verify_enforcement", lambda self, prefix: None)
    return wrapper


def test_required_claude_policy_reports_filesystem_network_and_inheritance(
        tmp_path: Path, sandbox_wrapper: Path):
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "network_destinations": ["api.example.test:443"],
                    "command": [str(sandbox_wrapper)]},
    })
    cmd = Harness("claude", {}).command(worktree=tmp_path, sandbox_policy=policy)
    settings = json.loads(cmd[cmd.index("--settings") + 1])

    assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(tmp_path), "$TMPDIR"]
    assert settings["sandbox"]["filesystem"]["denyWrite"] == ["//"]
    assert settings["sandbox"]["network"]["allowedDomains"] == ["api.example.test:443"]
    assert policy.report_env("test-sandbox+claude-native") == {
        "GARDEN_SANDBOX_MECHANISM": "test-sandbox+claude-native",
        "GARDEN_SANDBOX_ENFORCED": "1",
    }


@pytest.mark.parametrize("name, mode", [
    ("claude", "bypass"),
    ("codex", "dangerously-bypass-approvals-and-sandbox"),
])
def test_required_policy_rejects_harness_bypass(
        name: str, mode: str, tmp_path: Path, sandbox_wrapper: Path):
    with pytest.raises(SandboxError, match="bypass"):
        Harness(name, {"permission_mode": mode}).command(
            worktree=tmp_path,
            sandbox_policy=SandboxPolicy.from_config({
                "sandbox": {"required": True, "command": [str(sandbox_wrapper)]},
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


def test_required_policy_rejects_custom_harness(tmp_path: Path, sandbox_wrapper: Path):
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "network_destinations": ["example.test"],
                    "command": [str(sandbox_wrapper)]},
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


def test_required_policy_rejects_executable_without_capability_handshake(tmp_path: Path):
    policy = SandboxPolicy.from_config({"sandbox": {"required": True, "command": ["/bin/true"]}})
    with pytest.raises(SandboxError, match="capability report"):
        policy.command_argv("true", tmp_path)


def test_wrapper_receives_complete_policy(
        tmp_path: Path, sandbox_wrapper: Path):
    protected = tmp_path / "controller"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "network_destinations": ["approved.test:443"],
                    "command": [str(sandbox_wrapper)]},
    })
    argv, mechanism = policy.command_argv(
        "printf ok", worktree, readable_roots=[worktree], protected_roots=[protected],
    )
    payload = json.loads(argv[argv.index("--garden-sandbox-policy") + 1])
    assert payload == {
        "contract_version": 1, "writable_roots": [str(worktree)],
        "readable_roots": [str(worktree)], "protected_roots": [str(protected)],
        "network_destinations": ["approved.test:443"],
        "inherit_to_descendants": True, "resolve_symlinks": True,
    }
    assert mechanism == "test-sandbox"


def test_capability_attestation_without_enforcement_is_rejected(
        tmp_path: Path, sandbox_wrapper: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.undo()
    policy = SandboxPolicy.from_config({
        "sandbox": {"required": True, "command": [str(sandbox_wrapper)]},
    })
    with pytest.raises(SandboxError, match="enforcement challenge"):
        policy.command_argv("true", tmp_path)


def test_sandboxed_setup_writes_cache_marker_as_trusted_bookkeeping(
        tmp_path: Path, sandbox_wrapper: Path):
    from garden.runner.base import run_setup, setup_marker

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config = {"sandbox": {"required": True, "command": [str(sandbox_wrapper)]}}
    run_setup(worktree, {"command": "printf prepared > prepared.txt"}, config=config)
    assert (worktree / "prepared.txt").read_text() == "prepared"
    assert setup_marker(worktree).is_file()
