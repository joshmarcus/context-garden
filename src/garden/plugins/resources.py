"""Core-owned, bounded access to declarative data shipped by enabled plugins."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath

from .loading import LoadedPlugins
from .manifest import ResourceDeclaration, split_capability_name
from .registry import PluginError

SUPPORTED_MEDIA_TYPES = frozenset({"text/markdown", "text/plain", "application/json"})
EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".com", ".exe", ".js", ".mjs", ".ps1", ".py", ".pyc", ".rb", ".sh"})
MAX_PROFILE_FILES = 32


@dataclass(frozen=True)
class IncludedResource:
    text: str
    plugin_name: str
    plugin_version: str
    resource_name: str
    resource_version: str
    digest: str

    @property
    def provenance(self) -> dict[str, str]:
        return {
            "plugin_name": self.plugin_name, "plugin_version": self.plugin_version,
            "resource_name": self.resource_name, "resource_version": self.resource_version,
            "digest": self.digest,
        }


def _installed_bytes(distribution: str, path: str) -> bytes:
    dist = metadata.distribution(distribution)
    normalized = path.replace("\\", "/")
    files = {str(item).replace("\\", "/"): item for item in (dist.files or ())}
    if normalized not in files:
        raise PluginError(f"resource package path {path!r} is not declared by distribution {distribution!r}")
    try:
        return dist.locate_file(files[normalized]).read_bytes()
    except OSError as exc:
        raise PluginError(f"cannot read installed resource {path!r}: {exc}") from exc


class PluginResources:
    """Resolve only manifest-declared resources from explicitly enabled distributions."""

    def __init__(self, loaded: LoadedPlugins, reader: Callable[[str, str], bytes] = _installed_bytes):
        self.loaded = loaded
        self.reader = reader

    def read(self, reference: str, *, audience: str, product: str = "", public_product: bool = False,
             expected_kind: str = "context_pack") -> IncludedResource:
        plugin_name, resource_name = split_capability_name(reference)
        plugin = self.loaded.plugin(plugin_name)
        declaration = next((item for item in plugin.manifest.resources if item.name == resource_name), None)
        if declaration is None:
            raise PluginError(f"plugin {plugin_name!r} does not declare resource {resource_name!r}")
        self._authorize(declaration, plugin.manifest.distribution_version, audience, product,
                        public_product, expected_kind)
        content = self.reader(plugin.manifest.distribution, declaration.path)
        if len(content) > declaration.size_limit:
            raise PluginError(
                f"resource {reference!r} is {len(content)} bytes; declared limit is {declaration.size_limit}"
            )
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != declaration.digest:
            raise PluginError(f"resource {reference!r} digest changed: expected {declaration.digest}, observed {digest}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PluginError(f"resource {reference!r} is not valid UTF-8") from exc
        return IncludedResource(text, plugin_name, plugin.manifest.distribution_version,
                                resource_name, declaration.version, digest)

    @staticmethod
    def _authorize(resource: ResourceDeclaration, package_version: str, audience: str, product: str,
                   public_product: bool, expected_kind: str) -> None:
        try:
            resource.assert_complete(package_version)
        except ValueError as exc:
            raise PluginError(str(exc)) from exc
        if resource.kind != expected_kind:
            raise PluginError(f"resource {resource.name!r} is {resource.kind!r}, not {expected_kind!r}")
        if resource.media_type not in SUPPORTED_MEDIA_TYPES:
            raise PluginError(f"resource {resource.name!r} has unsupported media type {resource.media_type!r}")
        if audience not in resource.audience:
            raise PluginError(f"resource {resource.name!r} is not declared for audience {audience!r}")
        if product and product not in resource.products:
            raise PluginError(f"resource {resource.name!r} is not declared for product {product!r}")
        if public_product and resource.public_safe is not True:
            raise PluginError(f"resource {resource.name!r} is not declared safe for a public-product brief")


def profile_files(resource: IncludedResource) -> dict[PurePosixPath, str]:
    try:
        document = json.loads(resource.text)
    except json.JSONDecodeError as exc:
        raise PluginError(f"initialization profile {resource.resource_name!r} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"files"} or not isinstance(document["files"], dict):
        raise PluginError("initialization profile must contain exactly one 'files' mapping")
    if len(document["files"]) > MAX_PROFILE_FILES:
        raise PluginError(f"initialization profile may create at most {MAX_PROFILE_FILES} files")
    result: dict[PurePosixPath, str] = {}
    for raw_path, content in document["files"].items():
        path = PurePosixPath(str(raw_path))
        if path.is_absolute() or ".." in path.parts or not path.parts or not isinstance(content, str):
            raise PluginError(f"initialization profile has invalid logical file {raw_path!r}")
        if path.suffix.lower() in EXECUTABLE_SUFFIXES:
            raise PluginError(f"initialization profile may not install executable file {raw_path!r}")
        if path.parts[0] == ".garden":
            raise PluginError("initialization profile may not write scheduler state under .garden")
        result[path] = content
    return result


def apply_profile(root: Path, files: dict[PurePosixPath, str], *, overwrite: bool = False) -> tuple[list[Path], list[Path]]:
    resolved_root = root.resolve()
    targets = [root.joinpath(*path.parts) for path in files]
    for target in targets:
        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except ValueError as exc:
            raise PluginError(f"initialization profile path escapes the garden: {target}") from exc
    conflicts = [target for target in targets if target.exists() or target.is_symlink()]
    if conflicts and not overwrite:
        return [], conflicts
    created = []
    for logical, content in files.items():
        target = root.joinpath(*logical.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        created.append(target)
    return created, conflicts
