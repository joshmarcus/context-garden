"""The plugin manifest model, the registry's diagnostics, and inert discovery."""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import pytest

from garden.plugins import (
    API_VERSION,
    CAPABILITY_KINDS,
    CORE_ONLY_KINDS,
    ENTRY_POINT_GROUP,
    CapabilityDeclaration,
    CoreRange,
    DuplicateCapability,
    DuplicatePlugin,
    IncompatiblePlugin,
    PluginError,
    PluginManifest,
    PluginRegistry,
    ResourceDeclaration,
    UndeclaredCapability,
    UnknownPlugin,
    installed_entry_points,
    manifest_from_dict,
)

DIGEST = "sha256:" + "0" * 64

MANIFEST_DOCUMENT = {
    "name": "example-hosting",
    "distribution": "example-garden-plugin",
    "distribution_version": "1.2.0",
    "api_version": API_VERSION,
    "core_range": {"minimum": "0.3", "below": "1.0"},
    "summary": "An example host provider and its context pack.",
    "capabilities": [
        {
            "name": "example-hosting/host-provider",
            "kind": "host_provider",
            "entry_point": "example_garden_plugin.hosts:provider",
            "contract_version": "1",
            "summary": "Provision hosts from the example service.",
        },
        {
            "name": "example-hosting/onboarding-pack",
            "kind": "context_pack",
            "entry_point": "example_garden_plugin.packs:onboarding",
        },
    ],
    "config_schema": {
        "endpoint": {"type": "string", "required": True},
        "credentials": {"type": "object", "properties": {"token": {"type": "string"}}},
    },
    "redacted_config_keys": ["credentials.token"],
    "resources": [
        {
            "name": "onboarding-pack",
            "kind": "context_pack",
            "version": "1.2.0",
            "digest": DIGEST,
            "products": ["example-product"],
        },
    ],
}


def manifest(**overrides) -> PluginManifest:
    document = {**MANIFEST_DOCUMENT, **overrides}
    return manifest_from_dict(document)


def test_manifest_document_parses_into_an_immutable_validated_value() -> None:
    parsed = manifest()

    assert parsed.name == "example-hosting"
    assert parsed.identity == "example-garden-plugin==1.2.0"
    assert parsed.api_version == API_VERSION
    assert parsed.core_range == CoreRange(minimum="0.3", below="1.0")
    assert str(parsed.core_range) == ">=0.3,<1.0"
    assert [item.name for item in parsed.capabilities] == [
        "example-hosting/host-provider",
        "example-hosting/onboarding-pack",
    ]
    assert parsed.capability("example-hosting/host-provider").kind == "host_provider"
    assert parsed.capability("example-hosting/host-provider").local_name == "host-provider"
    assert parsed.redacted_config_keys == ("credentials.token",)
    assert parsed.resources[0].products == ("example-product",)
    assert parsed.resources[0].digest == DIGEST

    with pytest.raises(AttributeError):
        parsed.name = "renamed"
    with pytest.raises(TypeError):
        parsed.config_schema["endpoint"] = {}
    with pytest.raises(TypeError):
        parsed.config_schema["credentials"]["properties"] = {}


def test_manifest_supports_a_range_with_no_upper_bound() -> None:
    parsed = manifest(core_range={"minimum": "0.3"})

    assert str(parsed.core_range) == ">=0.3"
    assert parsed.core_range.contains("9.9.9")
    assert not parsed.core_range.contains("0.2.9")


def test_manifest_default_config_schema_is_constructible_and_immutable() -> None:
    parsed = PluginManifest(
        name="example-hosting",
        distribution="example-garden-plugin",
        distribution_version="1.2.0",
        api_version=API_VERSION,
        core_range=CoreRange(minimum="0.3"),
    )

    assert parsed.config_schema == {}
    with pytest.raises(TypeError):
        parsed.config_schema["endpoint"] = {"type": "string"}


def test_core_range_rejects_an_invalid_minimum_without_an_upper_bound() -> None:
    with pytest.raises(ValueError, match="numeric release"):
        CoreRange(minimum="not-a-version")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "Example Hosting"}, "plugin name must be a lowercase slug"),
        ({"distribution": "not a distribution"}, "distribution must be a distribution name"),
        ({"distribution_version": "latest"}, "numeric release"),
        ({"core_range": {"minimum": "1.0", "below": "1.0"}}, "is empty"),
        ({"core_range": "0.3"}, "core_range must be a mapping"),
        ({"config_schema": ["endpoint"]}, "config_schema must be a mapping"),
        ({"capabilities": "example-hosting/host-provider"}, "capabilities must be a list"),
        ({"redacted_config_keys": ["Token"]}, "dotted lowercase path"),
        ({"redacted_config_keys": ["secret"]}, "configuration schema does not declare"),
        ({"resources": [{"name": "pack", "kind": "context_pack",
                         "version": "1.0", "digest": "deadbeef"}]}, "sha256:"),
        ({"resources": [{"name": "pack", "kind": "context_pack", "version": "1.0",
                         "digest": DIGEST, "product": "example"}]}, "unsupported resource fields"),
        ({"summary": "s" * 201}, "longer than 200 characters"),
    ],
)
def test_manifest_rejects_malformed_documents(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        manifest(**overrides)


def test_manifest_rejects_unknown_and_misspelled_fields() -> None:
    with pytest.raises(ValueError, match=r"unsupported manifest fields: \['plugin_api'\]"):
        manifest(plugin_api="garden.plugins/v1")
    with pytest.raises(ValueError, match=r"unsupported capability fields: \['type'\]"):
        manifest(capabilities=[{"name": "example-hosting/host-provider", "kind": "host_provider",
                                "entry_point": "pkg:provider", "type": "host"}])


def test_capability_names_are_namespaced_under_their_own_plugin() -> None:
    with pytest.raises(ValueError, match="must be '<plugin>/<capability>'"):
        manifest(capabilities=[{"name": "host-provider", "kind": "host_provider",
                                "entry_point": "pkg:provider"}])
    with pytest.raises(ValueError, match="must be namespaced under its own plugin"):
        manifest(capabilities=[{"name": "other-plugin/host-provider", "kind": "host_provider",
                                "entry_point": "pkg:provider"}])


def test_manifest_rejects_a_capability_declared_twice() -> None:
    with pytest.raises(ValueError, match="declares capability .* twice"):
        manifest(capabilities=[
            {"name": "example-hosting/host-provider", "kind": "host_provider",
             "entry_point": "pkg:one"},
            {"name": "example-hosting/host-provider", "kind": "host_provider",
             "entry_point": "pkg:two"},
        ])


def test_manifest_rejects_an_unloadable_entry_point_string() -> None:
    with pytest.raises(ValueError, match="entry_point must be 'module:attribute'"):
        manifest(capabilities=[{"name": "example-hosting/host-provider",
                                "kind": "host_provider",
                                "entry_point": "rm -rf /; import pkg"}])


def test_manifest_rejects_an_entry_point_without_an_attribute() -> None:
    with pytest.raises(ValueError, match="entry_point must be 'module:attribute'"):
        manifest(capabilities=[{"name": "example-hosting/host-provider",
                                "kind": "host_provider",
                                "entry_point": "module_only"}])


def test_manifest_rejects_a_misspelled_nested_redaction_path() -> None:
    with pytest.raises(ValueError, match="configuration schema does not declare"):
        manifest(redacted_config_keys=["credentials.misspelled"])


def test_registry_indexes_manifests_and_resolves_declared_references() -> None:
    registry = PluginRegistry([manifest()], core_version="0.3.1")

    assert len(registry) == 1
    assert registry.plugin_names == ("example-hosting",)
    assert registry.manifest("example-hosting").identity == "example-garden-plugin==1.2.0"
    assert [item.name for item in registry.capabilities()] == [
        "example-hosting/host-provider",
        "example-hosting/onboarding-pack",
    ]
    assert [item.name for item in registry.capabilities("context_pack")] == [
        "example-hosting/onboarding-pack",
    ]
    resolved = registry.resolve(["example-hosting/host-provider"])
    assert resolved[0].kind == "host_provider"
    assert resolved[0].entry_point == "example_garden_plugin.hosts:provider"


def test_registry_reports_a_duplicate_plugin_name_with_both_distributions() -> None:
    other = manifest(distribution="other-garden-plugin", distribution_version="2.0.0")

    with pytest.raises(DuplicatePlugin) as failure:
        PluginRegistry([manifest(), other], core_version="0.3.1")

    assert "example-garden-plugin==1.2.0" in str(failure.value)
    assert "other-garden-plugin==2.0.0" in str(failure.value)


def test_registry_reports_a_capability_name_claimed_by_two_plugins() -> None:
    shared = CapabilityDeclaration(
        name="example-hosting/host-provider",
        kind="host_provider",
        entry_point="pkg:provider",
    )
    first = PluginManifest(
        name="example-hosting",
        distribution="example-garden-plugin",
        distribution_version="1.2.0",
        api_version=API_VERSION,
        core_range=CoreRange(minimum="0.3"),
        capabilities=(shared,),
    )
    second = PluginManifest(
        name="second-hosting",
        distribution="second-garden-plugin",
        distribution_version="1.0.0",
        api_version=API_VERSION,
        core_range=CoreRange(minimum="0.3"),
        capabilities=(),
    )
    # Manifest validation refuses another plugin's namespace, so the collision is planted
    # past it: the registry keeps its own index invariant whatever hands it declarations.
    object.__setattr__(second, "capabilities", (shared,))

    with pytest.raises(DuplicateCapability, match="declared by both 'example-hosting'"):
        PluginRegistry([first, second], core_version="0.3.1")


def test_registry_reports_a_missing_plugin_and_lists_what_is_registered() -> None:
    registry = PluginRegistry([manifest()], core_version="0.3.1")

    with pytest.raises(UnknownPlugin) as failure:
        registry.capability("absent-plugin/host-provider")

    assert "registered plugins: ['example-hosting']" in str(failure.value)
    with pytest.raises(UnknownPlugin, match="no plugin named 'absent-plugin'"):
        registry.manifest("absent-plugin")


def test_registry_reports_an_undeclared_capability_and_lists_the_declared_ones() -> None:
    registry = PluginRegistry([manifest()], core_version="0.3.1")

    with pytest.raises(UndeclaredCapability) as failure:
        registry.resolve(["example-hosting/runner"])

    assert "does not declare capability 'example-hosting/runner'" in str(failure.value)
    assert "example-hosting/host-provider" in str(failure.value)


def test_registry_reports_a_malformed_capability_reference() -> None:
    registry = PluginRegistry([manifest()], core_version="0.3.1")

    with pytest.raises(PluginError, match="must be '<plugin>/<capability>'"):
        registry.capability("example-hosting")


def test_registry_rejects_an_unsupported_plugin_api_version() -> None:
    with pytest.raises(IncompatiblePlugin) as failure:
        PluginRegistry([manifest(api_version="garden.plugins/v2")], core_version="0.3.1")

    assert "targets plugin API 'garden.plugins/v2'" in str(failure.value)
    assert API_VERSION in str(failure.value)


def test_registry_rejects_a_plugin_that_does_not_support_this_core() -> None:
    with pytest.raises(IncompatiblePlugin) as failure:
        PluginRegistry([manifest(core_range={"minimum": "0.9"})], core_version="0.3.1")

    assert "supports core >=0.9" in str(failure.value)
    assert "this core is 0.3.1" in str(failure.value)


def test_registry_rejects_a_plugin_at_or_past_its_upper_bound() -> None:
    with pytest.raises(IncompatiblePlugin, match="supports core >=0.1,<0.3"):
        PluginRegistry([manifest(core_range={"minimum": "0.1", "below": "0.3"})],
                       core_version="0.3.1")


def test_registry_rejects_a_core_version_it_cannot_compare() -> None:
    with pytest.raises(ValueError, match="numeric release"):
        PluginRegistry([manifest()], core_version="unreleased")


def test_core_owned_policy_cannot_be_declared_as_a_capability() -> None:
    for kind in sorted(CORE_ONLY_KINDS):
        with pytest.raises(ValueError, match="the core owns it"):
            manifest(capabilities=[{"name": "example-hosting/policy", "kind": kind,
                                    "entry_point": "pkg:policy"}])
    assert not CORE_ONLY_KINDS & CAPABILITY_KINDS
    with pytest.raises(ValueError, match="unsupported kind 'anything'"):
        manifest(capabilities=[{"name": "example-hosting/policy", "kind": "anything",
                                "entry_point": "pkg:policy"}])


def test_the_public_plugin_surface_offers_no_core_policy_hook() -> None:
    registry = PluginRegistry([manifest()], core_version="0.3.1")
    reserved = ("fence", "protected", "validation_plan", "approve", "human", "merge", "status")
    surfaces = [
        *dir(registry),
        *dir(registry.manifest("example-hosting")),
        *dir(registry.capability("example-hosting/host-provider")),
    ]

    assert not [name for name in surfaces
                if not name.startswith("_") and any(word in name for word in reserved)]
    # A capability is described, never supplied: the registry hands back strings.
    assert isinstance(registry.capability("example-hosting/host-provider").entry_point, str)
    assert not [name for name in dir(registry) if name in ("load", "instantiate", "activate")]


def test_a_garden_with_no_plugin_configuration_is_unchanged() -> None:
    from garden.config import DEFAULTS

    assert "plugins" not in DEFAULTS


def test_a_config_without_plugins_keeps_its_behavior_and_an_empty_registry(tmp_path) -> None:
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text("name: example-garden\nmax_parallel: 2\n")
    config = Config.load(tmp_path)
    registry = PluginRegistry()

    assert config.get("name") == "example-garden"
    assert config.get("max_parallel") == 2
    assert config.get("plugins") is None
    assert len(registry) == 0
    assert registry.plugin_names == ()
    assert registry.capabilities() == ()
    with pytest.raises(UnknownPlugin, match=r"registered plugins: \[\]"):
        registry.capability("example-hosting/host-provider")


def _install_failing_plugin_distribution(root: Path) -> None:
    """Write a distribution whose plugin module raises the moment it is imported."""
    (root / "example_broken_plugin.py").write_text(
        'raise RuntimeError("importing this plugin module must never happen")\n'
    )
    dist_info = root / "example_garden_plugin-1.2.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: example-garden-plugin\nVersion: 1.2.0\n"
    )
    (dist_info / "entry_points.txt").write_text(
        f"[{ENTRY_POINT_GROUP}]\nexample-hosting = example_broken_plugin:MANIFEST\n"
    )


def test_discovery_enumerates_installed_metadata_without_importing_plugin_code(
    tmp_path, monkeypatch
) -> None:
    _install_failing_plugin_distribution(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "example_broken_plugin" not in sys.modules

    found = [item for item in installed_entry_points() if item.name == "example-hosting"]

    assert len(found) == 1
    assert found[0].value == "example_broken_plugin:MANIFEST"
    assert found[0].distribution == "example-garden-plugin"
    assert found[0].distribution_version == "1.2.0"
    assert found[0].identity == "example-garden-plugin==1.2.0"
    # The guarantee, not a side effect: enumeration read metadata and imported nothing.
    assert "example_broken_plugin" not in sys.modules

    # And the assertion above has teeth, because loading that entry point does fail.
    entry = next(item for item in metadata.entry_points(group=ENTRY_POINT_GROUP)
                 if item.name == "example-hosting")
    with pytest.raises(RuntimeError, match="must never happen"):
        entry.load()
    sys.modules.pop("example_broken_plugin", None)


def test_discovery_of_an_unknown_group_is_empty_and_harmless() -> None:
    assert installed_entry_points("garden.plugins.absent") == ()


def test_a_resource_is_declared_safe_only_for_the_products_it_names() -> None:
    resource = ResourceDeclaration(
        name="onboarding-pack", kind="context_pack", version="1.2.0", digest=DIGEST,
    )

    assert resource.products == ()
    with pytest.raises(ValueError, match="product must be a lowercase slug"):
        ResourceDeclaration(name="onboarding-pack", kind="context_pack", version="1.2.0",
                            digest=DIGEST, products=("Example Product",))
