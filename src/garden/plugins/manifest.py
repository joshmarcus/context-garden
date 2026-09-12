"""Immutable, validated plugin manifests: the public plugin identity and capability contract.

A manifest is data.  Nothing in this module imports, instantiates or executes plugin code:
a capability's ``entry_point`` is carried as an opaque string that a later, explicitly
enabled loading step resolves.  The set of capability kinds is closed, so a plugin can
extend the parts of the loop the core delegates and no others; the worktree fence,
protected paths, the frozen validation plan and human-only actions stay in core.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, TypeVar

API_VERSION = "garden.plugins/v1"
SUPPORTED_API_VERSIONS = frozenset({API_VERSION})

#: The kinds of capability a plugin may declare.  The list is closed on purpose: adding a
#: kind is a reviewed change to the core, not something a manifest can assert.
CAPABILITY_KINDS = frozenset({
    "host_provider",
    "runner_transport",
    "source_control_provider",
    "check_provider",
    "doctor_check",
    "context_pack",
    "init_profile",
})

#: Kinds a manifest may never claim, named so the diagnostic says why rather than only
#: that the kind is unknown.  These decisions stay with the core and the human operator.
CORE_ONLY_KINDS = frozenset({
    "human_action",
    "merge_policy",
    "protected_paths",
    "task_status",
    "validation_plan",
    "worktree_fence",
})

_MAX_DEPTH = 8
_MAX_ITEMS = 64
_SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_KIND = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_RELEASE = re.compile(r"(\d+(?:\.\d+){0,3})")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CONFIG_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_DOTTED_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_ENTRY_POINT = re.compile(rf"{_DOTTED_NAME}(?::{_DOTTED_NAME})?")

T = TypeVar("T")


def release(version: str) -> tuple[int, ...]:
    """The numeric release used for every version comparison, padded to four segments.

    Pre-release and local suffixes are ignored rather than ordered: a supported range is
    written against released versions, so ``0.4.0rc1`` compares equal to ``0.4.0``.
    """
    match = _RELEASE.match(version.strip()) if isinstance(version, str) else None
    if not match:
        raise ValueError(
            f"version must start with a numeric release such as '1.2.3'; got {version!r}"
        )
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def split_capability_name(name: str) -> tuple[str, str]:
    """Split a namespaced capability name into its plugin and local parts."""
    plugin, separator, local = str(name).partition("/")
    if not separator or not _SLUG.fullmatch(plugin) or not _SLUG.fullmatch(local):
        raise ValueError(
            f"capability name must be '<plugin>/<capability>' in lowercase slugs; got {name!r}"
        )
    return plugin, local


def _slug(value: Any, *, what: str, limit: int = 64) -> str:
    text = str(value)
    if not _SLUG.fullmatch(text) or len(text) > limit:
        raise ValueError(
            f"{what} must be a lowercase slug such as 'example-hosting'; got {value!r}"
        )
    return text


def _freeze(value: Any, *, where: str, depth: int = 0) -> Any:
    """Return an immutable copy of declarative JSON data, or say what is unsupported."""
    if depth > _MAX_DEPTH:
        raise ValueError(f"{where} is nested deeper than {_MAX_DEPTH} levels")
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise ValueError(f"{where} may hold at most {_MAX_ITEMS} keys")
        for key in value:
            if not isinstance(key, str):
                raise ValueError(f"{where} keys must be strings; got {key!r}")
        return MappingProxyType({
            key: _freeze(item, where=f"{where}.{key}", depth=depth + 1)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_ITEMS:
            raise ValueError(f"{where} may hold at most {_MAX_ITEMS} items")
        return tuple(_freeze(item, where=where, depth=depth + 1) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(
        f"{where} may only contain declarative JSON data; found {type(value).__name__}"
    )


@dataclass(frozen=True)
class CoreRange:
    """The core versions a plugin distribution declares support for."""

    minimum: str
    below: str = ""

    def __post_init__(self) -> None:
        if self.below and release(self.below) <= release(self.minimum):
            raise ValueError(f"supported core range {self} is empty: 'below' must exceed 'minimum'")

    def contains(self, version: str) -> bool:
        found = release(version)
        return found >= release(self.minimum) and (not self.below or found < release(self.below))

    def __str__(self) -> str:
        return f">={self.minimum}" + (f",<{self.below}" if self.below else "")


@dataclass(frozen=True)
class CapabilityDeclaration:
    """One namespaced capability a plugin offers, described before anything is built."""

    name: str
    kind: str
    entry_point: str
    contract_version: str = "1"
    summary: str = ""

    def __post_init__(self) -> None:
        split_capability_name(self.name)
        if self.kind in CORE_ONLY_KINDS:
            raise ValueError(
                f"capability {self.name!r} may not declare kind {self.kind!r}: "
                "the core owns it and no plugin can replace it"
            )
        if self.kind not in CAPABILITY_KINDS:
            raise ValueError(
                f"capability {self.name!r} has unsupported kind {self.kind!r}; "
                f"supported kinds: {sorted(CAPABILITY_KINDS)}"
            )
        if not _ENTRY_POINT.fullmatch(str(self.entry_point)) or len(self.entry_point) > 256:
            raise ValueError(
                f"capability {self.name!r} entry_point must be 'module:attribute'; "
                f"got {self.entry_point!r}"
            )
        release(self.contract_version)
        if len(self.summary) > 200:
            raise ValueError(f"capability {self.name!r} summary is longer than 200 characters")

    @property
    def plugin(self) -> str:
        return split_capability_name(self.name)[0]

    @property
    def local_name(self) -> str:
        return split_capability_name(self.name)[1]


@dataclass(frozen=True)
class ResourceDeclaration:
    """A file a plugin ships, with the provenance a later slice serves it against.

    ``products`` is the closed list of products the resource is declared safe for.  An
    empty list means no product, not every product.
    """

    name: str
    kind: str
    version: str
    digest: str
    products: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _slug(self.name, what="resource name")
        if not _KIND.fullmatch(str(self.kind)) or len(self.kind) > 64:
            raise ValueError(
                f"resource {self.name!r} kind must be a lowercase name such as "
                f"'context_pack'; got {self.kind!r}"
            )
        release(self.version)
        if not _DIGEST.fullmatch(str(self.digest)):
            raise ValueError(
                f"resource {self.name!r} digest must be 'sha256:<64 hex characters>'; "
                f"got {self.digest!r}"
            )
        products = tuple(str(product) for product in self.products)
        for product in products:
            _slug(product, what=f"resource {self.name!r} product")
        object.__setattr__(self, "products", products)


@dataclass(frozen=True)
class PluginManifest:
    """Everything the core knows about a plugin before any capability is instantiated."""

    name: str
    distribution: str
    distribution_version: str
    api_version: str
    core_range: CoreRange
    capabilities: tuple[CapabilityDeclaration, ...] = ()
    config_schema: Mapping[str, Any] = MappingProxyType({})
    redacted_config_keys: tuple[str, ...] = ()
    resources: tuple[ResourceDeclaration, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        _slug(self.name, what="plugin name")
        if not _DISTRIBUTION.fullmatch(str(self.distribution)):
            raise ValueError(
                f"plugin {self.name!r} distribution must be a distribution name such as "
                f"'example-garden-plugin'; got {self.distribution!r}"
            )
        release(self.distribution_version)
        if not isinstance(self.core_range, CoreRange):
            raise ValueError(f"plugin {self.name!r} core_range must be a CoreRange")
        if len(self.summary) > 200:
            raise ValueError(f"plugin {self.name!r} summary is longer than 200 characters")
        if not isinstance(self.config_schema, Mapping):
            raise ValueError(
                f"plugin {self.name!r} config_schema must be a mapping of option names"
            )
        object.__setattr__(self, "capabilities", self._checked_capabilities())
        object.__setattr__(
            self, "config_schema", _freeze(self.config_schema, where="config_schema")
        )
        object.__setattr__(self, "redacted_config_keys", self._checked_redactions())
        object.__setattr__(self, "resources", self._checked_resources())

    def _checked_capabilities(self) -> tuple[CapabilityDeclaration, ...]:
        declared: dict[str, CapabilityDeclaration] = {}
        for capability in self.capabilities:
            if not isinstance(capability, CapabilityDeclaration):
                raise ValueError(
                    f"plugin {self.name!r} capabilities must be CapabilityDeclaration values"
                )
            if capability.plugin != self.name:
                raise ValueError(
                    f"capability {capability.name!r} must be namespaced under its own plugin "
                    f"{self.name!r}"
                )
            if capability.name in declared:
                raise ValueError(
                    f"plugin {self.name!r} declares capability {capability.name!r} twice"
                )
            declared[capability.name] = capability
        return tuple(declared.values())

    def _checked_redactions(self) -> tuple[str, ...]:
        keys: list[str] = []
        for key in self.redacted_config_keys:
            text = str(key)
            if not _CONFIG_KEY.fullmatch(text):
                raise ValueError(
                    f"plugin {self.name!r} redacted config key must be a dotted lowercase "
                    f"path such as 'credentials.token'; got {key!r}"
                )
            if text.split(".", 1)[0] not in self.config_schema:
                raise ValueError(
                    f"plugin {self.name!r} redacts {text!r}, which its configuration schema "
                    "does not declare"
                )
            if text in keys:
                raise ValueError(f"plugin {self.name!r} redacts {text!r} twice")
            keys.append(text)
        return tuple(keys)

    def _checked_resources(self) -> tuple[ResourceDeclaration, ...]:
        declared: dict[str, ResourceDeclaration] = {}
        for resource in self.resources:
            if not isinstance(resource, ResourceDeclaration):
                raise ValueError(
                    f"plugin {self.name!r} resources must be ResourceDeclaration values"
                )
            if resource.name in declared:
                raise ValueError(
                    f"plugin {self.name!r} declares resource {resource.name!r} twice"
                )
            declared[resource.name] = resource
        return tuple(declared.values())

    @property
    def identity(self) -> str:
        """The exact distribution a later slice pins and records as provenance."""
        return f"{self.distribution}=={self.distribution_version}"

    def capability(self, name: str) -> CapabilityDeclaration | None:
        return next((item for item in self.capabilities if item.name == name), None)


def _construct(cls: type[T], values: Mapping[str, Any], *, where: str) -> T:
    allowed = {item.name for item in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unsupported {where} fields: {unknown}")
    return cls(**values)


def _listed(data: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    value = data.get(key, ())
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list; got {type(value).__name__}")
    return tuple(value)


def manifest_from_dict(value: Mapping[str, Any]) -> PluginManifest:
    """Parse one manifest mapping strictly, rejecting misspelled or unknown fields."""
    if not isinstance(value, Mapping):
        raise ValueError("a plugin manifest must be a mapping")
    data = dict(value)
    raw_range = data.get("core_range")
    if not isinstance(raw_range, Mapping):
        raise ValueError("core_range must be a mapping with 'minimum' and optional 'below'")
    data["core_range"] = _construct(CoreRange, raw_range, where="core_range")
    data["capabilities"] = tuple(
        _construct(CapabilityDeclaration, item, where="capability")
        if isinstance(item, Mapping) else item
        for item in _listed(data, "capabilities")
    )
    data["resources"] = tuple(
        _construct(ResourceDeclaration, item, where="resource")
        if isinstance(item, Mapping) else item
        for item in _listed(data, "resources")
    )
    data["redacted_config_keys"] = _listed(data, "redacted_config_keys")
    return _construct(PluginManifest, data, where="manifest")
