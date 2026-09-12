# Plugin manifests and the registry

A plugin is an installed Python distribution that offers context-garden extra capabilities —
a host provider, a runner transport, a source-control or check provider, a doctor check, a
context pack, an initialization profile. This page is the contract for describing one.

It covers only description and indexing. Nothing here loads a plugin: choosing which
installed plugins a garden enables, validating their configuration and running a capability
are separate, later steps. Until then a manifest is data and the registry is an index.

Everything below lives in `garden.plugins` (`src/garden/plugins/`).

## What a plugin looks like from outside

Two facts, kept apart on purpose:

- **The distribution** is what a person installs and pins: a name and a version, for example
  `example-garden-plugin==1.2.0`. That pair is the plugin's provenance.
- **The plugin** is the logical extension the distribution provides: a lowercase slug such as
  `example-hosting`, which owns a namespace for the capabilities it declares.

One distribution provides one plugin per manifest, and two installed distributions may not
provide the same plugin name — the registry reports both distributions when they do.

## The manifest

A manifest is an immutable, validated value: `PluginManifest`, or `manifest_from_dict` for a
mapping loaded from a document. Unknown or misspelled fields are rejected rather than ignored.

```python
from garden.plugins import manifest_from_dict

MANIFEST = manifest_from_dict({
    "name": "example-hosting",
    "distribution": "example-garden-plugin",
    "distribution_version": "1.2.0",
    "api_version": "garden.plugins/v1",
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
            "digest": "sha256:<64 hex characters>",
            "products": ["example-product"],
        },
    ],
})
```

| field | meaning |
|---|---|
| `name` | the plugin's identity and capability namespace: a lowercase slug (`example-hosting`) |
| `distribution`, `distribution_version` | the installed distribution this plugin came from; `manifest.identity` renders them as `name==version` |
| `api_version` | the plugin API this manifest is written against: `garden.plugins/v1` |
| `core_range` | the core versions the plugin supports: `{"minimum": "0.3", "below": "1.0"}`; `below` is exclusive and may be omitted for no upper bound |
| `capabilities` | the namespaced capabilities the plugin declares (below) |
| `config_schema` | a declarative description of the plugin's own configuration block: option name to JSON-shaped description |
| `redacted_config_keys` | dotted paths inside that block whose values must never be printed, logged or sent anywhere |
| `resources` | files the plugin ships, with the provenance they are served against (below) |
| `summary` | one short line for an operator; 200 characters at most |

The manifest is immutable in depth: `config_schema` is exposed as a read-only mapping, lists
become tuples, and only JSON-shaped data is accepted, so no plugin object can be smuggled in
as configuration and no consumer can mutate what another consumer read.

### Versions

Three versions answer three different questions, and a manifest carries all three:

- `api_version` — *does this core understand the manifest at all?* It is an exact match
  against `garden.plugins/v1` today. A future `v2` core states which versions it supports.
- `core_range` — *does the plugin support this core?* Comparison uses the numeric release
  only, padded to four segments, so `0.3` and `0.3.0` are the same version and `0.4.0rc1`
  compares equal to `0.4.0`. Pre-release ordering is deliberately not modelled: a range is
  written against released versions.
- `contract_version` on each capability — *which revision of that capability's own interface
  does it implement?* Capability contracts version independently of the distribution, so one
  plugin can carry a `host_provider` at contract 2 and a `context_pack` at contract 1.

`distribution_version` is provenance rather than compatibility: it is what a person pins and
what a later slice records against an external action.

### Capability namespaces

A capability name is `<plugin>/<capability>`, both parts lowercase slugs, and the namespace
must be the declaring plugin's own name. `example-hosting/host-provider` is valid in the
manifest above; `other-plugin/host-provider` is refused there. Namespacing means two plugins
can both offer a `host-provider` without a collision, and a reference in configuration always
says which plugin it means.

`kind` chooses from a closed list — `host_provider`, `runner_transport`,
`source_control_provider`, `check_provider`, `doctor_check`, `context_pack`, `init_profile`
(`garden.plugins.CAPABILITY_KINDS`). `entry_point` is an opaque `module:attribute` string:
the manifest checks its shape and nothing else resolves it here.

### What a plugin cannot declare

The extension surface is what the core delegates, and nothing more. The worktree fence,
protected paths, the frozen validation plan, human-only actions, merge policy and task status
stay with the core and the person operating it. Those are named in
`garden.plugins.CORE_ONLY_KINDS` so that claiming one is refused with the reason rather than
only as an unknown kind:

```
capability 'example-hosting/policy' may not declare kind 'worktree_fence':
the core owns it and no plugin can replace it
```

Because the kind list is closed, a new extension point is a reviewed change to the core — it
can never arrive as an assertion in a manifest.

### Resources and redaction

A resource declaration carries a `version` and a `sha256:` digest, so what a garden serves can
be checked against what was reviewed. `products` is the closed list of products the resource is
declared safe for; an empty list means *no* product, not every product.

`redacted_config_keys` names secrets inside the plugin's configuration block. Each key must be
declared in `config_schema` — redacting a key the schema does not mention is a typo, and the
manifest says so rather than silently redacting nothing.

## The registry

`PluginRegistry` indexes manifests and resolves capability references. It holds no factories
and calls nothing a plugin ships; every failure is raised at construction or lookup, before
anything is built.

```python
from garden.plugins import PluginRegistry

registry = PluginRegistry([MANIFEST])
registry.plugin_names                                  # ('example-hosting',)
registry.manifest("example-hosting").identity          # 'example-garden-plugin==1.2.0'
registry.capabilities("context_pack")                  # one declaration
registry.capability("example-hosting/host-provider")   # kind, entry_point, contract_version
registry.resolve(["example-hosting/host-provider"])    # or the first error below
```

A registry built with no manifests is the normal case: a garden with no plugin configuration
gets an empty registry, which changes nothing about how it runs.

Every diagnostic is an operator-facing subclass of `PluginError` and names both what is wrong
and what is present:

| error | raised when |
|---|---|
| `DuplicatePlugin` | two installed distributions provide the same plugin name; both identities are reported |
| `DuplicateCapability` | two plugins claim one namespaced capability |
| `UnknownPlugin` | a reference names a plugin that is not registered; the registered names are listed |
| `UndeclaredCapability` | the plugin is registered but its manifest does not declare that capability; the declared ones are listed |
| `IncompatiblePlugin` | the manifest targets an unsupported `api_version`, or a `core_range` this core is outside |
| `PluginError` | a reference is not of the form `<plugin>/<capability>` |

## Discovery does not run plugin code

`installed_entry_points()` enumerates the installed distributions advertising the
`garden.plugins` entry-point group. It reads distribution metadata only: `EntryPoint.load()` is
never called and no plugin module is imported.

```python
from garden.plugins import installed_entry_points

for found in installed_entry_points():
    found.name, found.value, found.identity
    # 'example-hosting', 'example_garden_plugin:MANIFEST', 'example-garden-plugin==1.2.0'
```

A `DiscoveredEntryPoint` is strings, not a callable: there is no `load()` on it to reach for.
Two consequences matter.

- Merely having a distribution installed cannot execute its code. Importing happens only in a
  later, explicit step for a plugin a garden's configuration has enabled.
- A plugin that is broken on import is still discoverable, so the enumeration that would report
  it cannot be broken by it. `tests/test_plugins.py` proves this with a distribution whose
  module raises on import: discovery lists it, `sys.modules` never gains it, and the test then
  loads the same entry point itself to show the failure is real.

## Advertising a plugin

A distribution advertises its manifest under the `garden.plugins` group. Only the name and the
`module:attribute` string are read by discovery.

```toml
# pyproject.toml, in an example plugin distribution
[project.entry-points."garden.plugins"]
example-hosting = "example_garden_plugin:MANIFEST"
```

Point it at the module-level `PluginManifest` the plugin defines. Keep that module cheap to
import for when a later step does enable the plugin, and keep the manifest itself free of
anything organization-specific: it is read by whoever installs the distribution.
