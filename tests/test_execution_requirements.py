from pathlib import Path

import pytest
import yaml

from garden.config import Config
from garden.hosts.models import HostRequirements
from garden.model import (
    ExecutionRequirements,
    Phase,
    Task,
    parse_execution_requirements,
)


def requirement(**resources):
    return {
        "capabilities": {"all_of": ["tool.sql-client"]},
        "resources": resources,
        "preferences": {"worker_configurations": ["restricted-gpu"]},
    }


def task_with(value=None):
    text = yaml.safe_dump({
        "id": "CG-1", "title": "one", "product": "demo", "phase": "p1",
        **({"execution_requirements": value} if value is not None else {}),
    }, sort_keys=False)
    return Task.parse(Path("task.md"), f"---\n{text}---\n\nbody\n")


def test_task_requirements_round_trip_and_no_requirement_compatibility():
    task = task_with(requirement(
        memory_mib=32768, vcpu=8,
        gpu={"count": 1, "vendor": "nvidia", "min_device_memory_mib": 24576,
             "features": ["cuda"]},
    ))
    reparsed = Task.parse(Path("task.md"), task.render())
    assert reparsed.execution_requirements == task.execution_requirements
    assert task_with().execution_requirements == ExecutionRequirements()
    assert "execution_requirements" not in task_with().to_frontmatter()


@pytest.mark.parametrize("value, message", [
    ({"capabilities": []}, "capabilities must contain only all_of"),
    ({"resources": False}, "resources has unknown fields"),
    ({"preferences": []}, "preferences has unknown fields"),
    ({"resources": {"memory_gib": 4}}, "unknown fields"),
    ({"resources": {"memory_mib": 1.5}}, "whole number"),
    ({"resources": {"vcpu": -1}}, "whole number"),
    ({"resources": {"gpu": {"count": 0}}}, "positive whole number"),
    ({"capabilities": {"all_of": ["SQL"]}}, "invalid name"),
])
def test_requirements_reject_unknown_fields_invalid_units_and_values(value, message):
    with pytest.raises(ValueError, match=message):
        parse_execution_requirements(value)


def test_policy_merge_is_monotonic_attributed_and_ceiling_enforced(tmp_path):
    config_data = {
        "capability_definitions": {
            "platform.linux": {"type": "platform", "description": "Linux",
                               "issuer": "operator", "privileged": False},
            "network.analytics-zone": {"type": "network", "description": "Synthetic zone",
                                       "issuer": "security", "privileged": True},
            "tool.sql-client": {"type": "tool", "description": "SQL client",
                                "issuer": "operator", "privileged": False},
        },
        "execution_limits": {"memory_mib": 20000, "vcpu": 16},
        "products": {"demo": {
            "execution_requirements": {
                "capabilities": {"all_of": ["platform.linux"]},
                "resources": {"memory_mib": 4096, "vcpu": 2},
            },
            "execution_limits": {"memory_mib": 32768, "vcpu": 8,
                                 "gpu": {"count": 1, "min_device_memory_mib": 24576}},
        }},
    }
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(config_data))
    config = Config.load(tmp_path)
    phase = Phase("demo", "p1", tmp_path, None, [], [], [], meta={
        "execution_requirements": {
            "capabilities": {"all_of": ["network.analytics-zone"]},
            "resources": {"memory_mib": 8192},
        }
    })
    effective = config.execution_requirements(task_with(requirement(
        memory_mib=16384, vcpu=4,
        gpu={"count": 1, "vendor": "nvidia", "min_device_memory_mib": 16384},
    )), phase)
    assert effective.capabilities == (
        "network.analytics-zone", "platform.linux", "tool.sql-client"
    )
    assert (effective.resources.memory_mib, effective.resources.vcpu) == (16384, 4)
    assert effective.preferred_worker_configurations == ("restricted-gpu",)
    assert effective.provenance == ("product:demo", "phase:demo/p1", "task")

    too_large = task_with({"resources": {"vcpu": 9}})
    with pytest.raises(ValueError, match="exceeds deployment ceiling"):
        config.execution_requirements(too_large, phase)
    too_much_memory = task_with({"resources": {"memory_mib": 21000}})
    with pytest.raises(ValueError, match="memory_mib requirement 21000.*ceiling 20000"):
        config.execution_requirements(too_much_memory, phase)


def test_unknown_capabilities_and_incompatible_gpu_bounds_fail_closed(tmp_path):
    (tmp_path / "garden.yaml").write_text("products:\n  demo: {}\n")
    config = Config.load(tmp_path)
    with pytest.raises(ValueError, match="unknown capability requirements"):
        config.execution_requirements(task_with(requirement(memory_mib=1)))

    product = parse_execution_requirements(
        {"resources": {"gpu": {"count": 1, "vendor": "amd"}}}, source="product"
    )
    task = parse_execution_requirements(
        {"resources": {"gpu": {"count": 1, "vendor": "nvidia"}}}, source="task"
    )
    from garden.model import merge_execution_requirements
    with pytest.raises(ValueError, match="incompatible GPU vendor"):
        merge_execution_requirements(product, task)


def test_legacy_host_requirements_use_the_canonical_capability_vocabulary():
    legacy = HostRequirements(
        activity="check", host_class="large", environment="linux",
        capabilities=("python", "tool.sql-client"), memory_mib=4096,
    )
    canonical = ExecutionRequirements.from_host_requirements(legacy)
    assert parse_execution_requirements(canonical.to_dict()).capabilities == legacy.capabilities
    projected = canonical.to_host_requirements(
        activity="check", host_class="large", environment="linux"
    )
    assert projected.capabilities == legacy.capabilities
    assert projected.memory_mib == legacy.memory_mib
