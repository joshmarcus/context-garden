from __future__ import annotations

from copy import deepcopy

import pytest
import yaml
from typer.testing import CliRunner

from garden.cli import app
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


def test_config_reports_editable_and_layered_setting_sources(tmp_path, monkeypatch):
    (tmp_path / "garden.yaml").write_text("max_parallel: 3\n")
    (tmp_path / "garden.work.yaml").write_text("max_parallel: 5\n")
    (tmp_path / "garden.local.yaml").write_text("max_parallel: 7\n")
    monkeypatch.setenv("GARDEN_ENV", "work")

    layered = Config.load(tmp_path)
    editable = layered.editable()

    assert editable.get("max_parallel") == 3
    assert editable.setting_source("max_parallel") == "garden.yaml"
    assert layered.get("max_parallel") == 7
    assert layered.setting_source("max_parallel") == "garden.local.yaml"
    assert layered.setting_source("auto_dispatch") == "default"


def test_config_reports_only_surviving_mapping_contributors(tmp_path):
    (tmp_path / "garden.yaml").write_text(
        "budgets:\n  alpha: 10\n  replaced: 5\n"
    )
    (tmp_path / "garden.work.yaml").write_text("budgets:\n  replaced: 8\n")
    (tmp_path / "garden.local.yaml").write_text("budgets:\n  replaced: 13\n")

    config = Config.load(tmp_path, env="work")

    assert config.get("budgets") == {"alpha": 10, "replaced": 13}
    assert config.setting_source("budgets") == "garden.yaml + garden.local.yaml"


def test_config_reports_winning_list_source(tmp_path):
    (tmp_path / "garden.yaml").write_text("github:\n  reviewers: [alice]\n")
    (tmp_path / "garden.local.yaml").write_text("github:\n  reviewers: [bob, cam]\n")

    config = Config.load(tmp_path)

    assert config.get("github.reviewers") == ["bob", "cam"]
    assert config.setting_source("github.reviewers") == "garden.local.yaml"


def test_plain_project_lock_keeps_inherited_value_source_separate(tmp_path):
    (tmp_path / "garden.yaml").write_text(
        "max_parallel: 3\nproducts:\n  demo:\n    configuration:\n"
        "      locks:\n        max_parallel: capacity policy\n"
    )
    (tmp_path / "garden.work.yaml").write_text("max_parallel: 7\n")

    config = Config.load(tmp_path, env="work")
    provenance = config.setting("max_parallel", "demo")

    assert provenance.locked and provenance.policy_source == (
        "products.demo.configuration.locks"
    )
    assert config.setting_source("max_parallel", "demo") == "garden.work.yaml"


def test_enforced_project_value_reports_its_policy_layer(tmp_path):
    (tmp_path / "garden.yaml").write_text(
        "max_parallel: 3\nproducts:\n  demo:\n    configuration:\n"
        "      locks:\n        max_parallel:\n          reason: capacity policy\n"
        "          value: 2\n"
    )
    (tmp_path / "garden.local.yaml").write_text(
        "products:\n  demo:\n    configuration:\n      locks:\n"
        "        max_parallel:\n          value: 1\n"
    )

    config = Config.load(tmp_path)

    assert config.setting("max_parallel", "demo").value == 1
    assert config.setting_source("max_parallel", "demo") == "garden.local.yaml"


def test_metadata_inventory_describes_every_value_on_configuration_page():
    displayed = {
        "max_parallel", "review_parallel", "auto_dispatch", "auto_revise", "stack",
        "operating_profile", "observe.profile", "observe.interval", "observe.digest_window",
        "observe.events", "observe.stuck_after", "observe.phases", "review.enabled",
        "review.max_rounds", "review.friction_after", "review.difficulty", "review.ladder",
        "retro.difficulty", "github.draft_pr", "budgets", "work_dir", "tick_interval",
        "resources.execution_cgroup", "dispatch_paused", "maintenance", "resource_status",
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
        {"products": {"p": {"configuration": {"overrides": {"tick_interval": 5}}}}},
        {"max_parallel": 0},
    ]
    for data in cases:
        (tmp_path / "garden.yaml").write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError):
            Config.load(tmp_path)


def test_legacy_global_and_product_review_count_floors_are_normalized(tmp_path):
    data = {
        "github": {"automerge_min_review_rounds": 3},
        "products": {"demo": {"automerge_min_review_rounds": 2}},
    }
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(data))

    with pytest.warns(UserWarning, match="normalized to 1") as warnings:
        config = Config.load(tmp_path)

    assert len(warnings) == 2
    assert config.get("github.automerge_min_review_rounds") == 1
    assert config.product("demo")["automerge_min_review_rounds"] == 1


def test_plain_lock_freezes_inherited_value_across_global_edits_and_reload(tmp_path):
    before = {
        "max_parallel": 4,
        "products": {"p": {"configuration": {"locks": {"max_parallel": "capacity"}}}},
    }
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(before))
    config = Config.load(tmp_path)
    assert config.setting("max_parallel", "p").value == 4

    with pytest.raises(PermissionError, match="capacity"):
        apply_changes(before, {"max_parallel": 5})
    with pytest.raises(PermissionError, match="capacity"):
        config.save_changes({"max_parallel": 5})

    # Loading a policy for the first time is valid: there is no earlier effective value to
    # preserve. Adoption by a running scheduler performs the before/after reload check.
    changed = deepcopy(before)
    changed["max_parallel"] = 5
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(changed))
    reloaded = Config.load(tmp_path)
    assert reloaded.setting("max_parallel", "p").value == 5


def test_saved_profile_selection_cannot_bypass_plain_lock_after_restart(garden):
    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["profiles"] = {"expanded": {"workers": 7}}
    data["products"]["demo"]["configuration"] = {
        "locks": {"max_parallel": {"reason": "fixed profile capacity"}},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    config = Config.load(garden)
    before = path.read_text()

    with pytest.raises(PermissionError, match="fixed profile capacity"):
        config.save_changes({"operating_profile": "expanded"})

    assert path.read_text() == before
    from garden.scheduler import Scheduler
    from garden.store import Store

    fresh = Scheduler(Store(garden))
    assert fresh.operating_profile_name() == ""
    assert fresh.effective("max_parallel", product="demo") == 2


def test_audit_redacts_sensitive_values_by_key():
    assert audit_value("service.token", "plain text") == "<redacted>"
    assert audit_value("max_parallel", 3) == 3


def test_saved_changes_are_atomic_layer_aware_and_reject_locked_reset(tmp_path):
    base = configured()
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(base))
    (tmp_path / "garden.work.yaml").write_text("auto_revise: false\n")
    config = Config.load(tmp_path, env="work")
    token = revision(config.data)

    updated = config.save_changes({"max_parallel": 6}, expected_revision=token)
    assert updated.get("max_parallel") == 6
    assert updated.get("auto_revise") is False
    assert yaml.safe_load((tmp_path / "garden.yaml").read_text())["max_parallel"] == 6

    contents = (tmp_path / "garden.yaml").read_text()
    with pytest.raises(RuntimeError, match="changed since"):
        updated.save_changes({"max_parallel": 7}, expected_revision=token)
    with pytest.raises(PermissionError, match="Protect shared capacity"):
        updated.save_changes({"max_parallel": None}, product="locked", reset=True)
    assert (tmp_path / "garden.yaml").read_text() == contents


def test_revision_ignores_scheduler_runtime_metadata():
    data = configured()
    with_runtime_metadata = {**data, "_notification_delivery_path": "/private/runtime/path"}

    assert revision(with_runtime_metadata) == revision(data)


def test_cli_saved_project_edit_and_locked_bypass_use_shared_boundary(garden, monkeypatch):
    monkeypatch.chdir(garden)
    monkeypatch.delenv("GARDEN_ROOT", raising=False)
    runner = CliRunner()

    result = runner.invoke(app, ["config", "set", "max_parallel", "3", "--product", "demo"])
    assert result.exit_code == 0, result.output
    assert Config.load(garden).setting("max_parallel", "demo").value == 3

    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["products"]["demo"]["configuration"]["locks"] = {
        "max_parallel": {"reason": "capacity policy"},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    result = runner.invoke(app, ["config", "reset", "max_parallel", "--product", "demo"])
    assert result.exit_code == 1
    assert "capacity policy" in result.output
    assert Config.load(garden).setting("max_parallel", "demo").value == 3

def test_browser_execution_defaults_off_and_supports_project_opt_in(tmp_path):
    (tmp_path / "garden.yaml").write_text("products:\n  web:\n    configuration:\n      overrides:\n        browser.enabled: true\n")

    config = Config.load(tmp_path)

    assert config.browser_enabled() is False
    assert config.browser_enabled("web") is True


def test_existing_config_without_browser_authority_stays_disabled(tmp_path):
    (tmp_path / "garden.yaml").write_text("name: existing\nproducts:\n  app: {}\n")

    assert Config.load(tmp_path).browser_enabled("app") is False
