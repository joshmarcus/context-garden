from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from garden.config import Config
from garden.configuration import (
    CONFIG_FIELDS,
    ApplyMode,
    ConfigScope,
    apply_changes,
    audit_value,
    revision,
)


def configured() -> dict:
    return {
        "max_parallel": 4,
        "auto_dispatch": True,
        "products": {
            "locked": {
                "configuration": {
                    "overrides": {"max_parallel": 2, "auto_dispatch": False},
                    "locks": {
                        "max_parallel": {"reason": "Protect shared capacity", "source": "team policy"},
                        "auto_dispatch": {"reason": "Release hold", "value": False},
                    },
                },
            },
            "open": {"configuration": {"overrides": {"max_parallel": 3}}},
        },
    }


def test_metadata_inventory_describes_every_value_on_configuration_page():
    displayed = {
        "max_parallel", "review_parallel", "auto_dispatch", "auto_revise", "stack",
        "operating_profile", "observe.profile", "observe.interval", "observe.digest_window",
        "observe.events", "observe.stuck_after", "observe.phases", "review.enabled",
        "review.max_rounds", "review.friction_after", "review.difficulty", "review.ladder",
        "retro.difficulty", "github.draft_pr", "budgets", "work_dir", "tick_interval",
        "dispatch_paused", "maintenance", "resource_status",
    }
    assert displayed <= CONFIG_FIELDS.keys()
    assert all(field.help and field.value_type and field.scopes for field in CONFIG_FIELDS.values())
    assert CONFIG_FIELDS["resource_status"].apply == ApplyMode.DERIVED
    assert CONFIG_FIELDS["tick_interval"].scopes == (ConfigScope.GLOBAL,)


def test_project_values_are_isolated_and_locked_values_have_provenance(tmp_path):
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(configured()))
    config = Config.load(tmp_path)

    locked = config.setting("max_parallel", "locked")
    assert (locked.value, locked.source, locked.locked) == (2, "project:locked", True)
    assert locked.reason == "Protect shared capacity" and locked.policy_source == "team policy"
    assert config.setting("max_parallel", "open").value == 3
    assert config.setting("max_parallel", "unknown").value == 4
    enforced = config.setting("auto_dispatch", "locked")
    assert enforced.value is False and enforced.source == "policy:locked"


def test_direct_edit_and_reset_cannot_bypass_lock_or_remove_policy():
    before = configured()
    with pytest.raises(PermissionError, match="Protect shared capacity"):
        apply_changes(before, {"max_parallel": 5}, product="locked")
    with pytest.raises(PermissionError, match="Protect shared capacity"):
        apply_changes(before, {"max_parallel": 5}, product="locked", reset=True)
    assert before == configured()


def test_global_and_profile_style_changes_cannot_alter_locked_effective_values():
    before = configured()
    global_edit = apply_changes(before, {"max_parallel": 8})
    assert Config(root=None, data=global_edit).setting("max_parallel", "locked").value == 2  # type: ignore[arg-type]
    assert Config(root=None, data=global_edit).setting("max_parallel", "open").value == 3  # type: ignore[arg-type]
    # A profile/runtime layer is upstream too: the explicit locked project value remains the
    # effective product value when that layer asks for a different global value.
    profiled = apply_changes(global_edit, {"max_parallel": 12})
    assert Config(root=None, data=profiled).setting("max_parallel", "locked").value == 2  # type: ignore[arg-type]


def test_batch_is_atomic_rejects_stale_writes_and_preserves_extensions():
    before = configured()
    before["extension"] = {"kept": [1, 2, 3]}
    snapshot = deepcopy(before)
    with pytest.raises(ValueError, match="at least 1"):
        apply_changes(before, {"auto_dispatch": False, "max_parallel": 0})
    assert before == snapshot

    token = revision(before)
    changed = apply_changes(before, {"max_parallel": 6}, expected_revision=token)
    assert changed["extension"] == before["extension"]
    with pytest.raises(RuntimeError, match="changed since"):
        apply_changes(changed, {"max_parallel": 7}, expected_revision=token)


def test_reload_rejects_inconsistent_locks_global_only_overrides_and_invalid_values(tmp_path):
    cases = [
        {"products": {"p": {"configuration": {"locks": {"max_parallel": "reason"}}}}},
        {"products": {"p": {"configuration": {"overrides": {"tick_interval": 5}}}}},
        {"max_parallel": 0},
    ]
    for data in cases:
        (tmp_path / "garden.yaml").write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError):
            Config.load(tmp_path)


def test_audit_redacts_sensitive_values_by_key():
    assert audit_value("service.token", "plain text") == "<redacted>"
    assert audit_value("max_parallel", 3) == 3
