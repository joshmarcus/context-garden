from __future__ import annotations

import ast
import os
import site
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "context-garden-example-plugin"


def _run(*command: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _build_wheel(source: Path, destination: Path, *, env: dict[str, str]) -> None:
    """Exercise the distribution's real pyproject without network or build isolation."""
    _run(
        sys.executable, "-m", "hatchling", "build", "--target", "wheel",
        "--directory", str(destination), cwd=source, env=env,
    )


def test_example_distribution_uses_only_public_plugin_imports() -> None:
    trees = [ast.parse(path.read_text()) for path in
             (EXAMPLE / "src/context_garden_example").glob("*.py")]
    garden_imports = [
        node.module
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("garden")
    ]
    assert garden_imports == ["garden.plugins", "garden.plugins"]


def test_external_distribution_lifecycle_and_failure_matrix(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    _build_wheel(ROOT, wheels, env=environment)
    _build_wheel(EXAMPLE, wheels, env=environment)
    built = {path.name for path in wheels.glob("*.whl")}
    assert any(name.startswith("context_garden-0.4.0-") for name in built)
    assert any(name.startswith("context_garden_example_plugin-0.1.0-") for name in built)

    venv = tmp_path / "venv"
    _run(sys.executable, "-m", "venv", "--system-site-packages", str(venv),
         cwd=tmp_path, env=environment)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_site = Path(_run(
        str(python), "-c", "import site; print(site.getsitepackages()[0])",
        cwd=tmp_path, env=environment,
    ).stdout.strip())
    dependency_site = next(Path(path) for path in site.getsitepackages()
                           if Path(path).name == "site-packages")
    (venv_site / "context-garden-test-dependencies.pth").write_text(str(dependency_site) + "\n")
    _run(
        str(python), "-m", "pip", "install", "--no-index", "--no-deps",
        "--force-reinstall", "--find-links", str(wheels), "context-garden==0.4.0",
        "context-garden-example-plugin==0.1.0", cwd=tmp_path, env=environment,
    )

    exercise = r'''
import dataclasses
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from garden.plugins import (
    IncompatiblePlugin, LoadedPlugin, LoadedPlugins, MalformedOutput, PluginConfigurationError,
    PluginError, PluginRegistry, PluginResources, UndeclaredCapability, apply_profile,
    inspect_lock, installed_entry_points, load_configured_plugins, profile_files,
    run_check_provider, run_doctor_checks, write_lock,
)

root = Path(sys.argv[1])
root.mkdir()
module = "context_garden_example"
assert module not in sys.modules
found = [item for item in installed_entry_points() if item.name == "example-tools"]
assert len(found) == 1 and found[0].identity == "context-garden-example-plugin==0.1.0"
assert module not in sys.modules  # metadata discovery is inert
assert load_configured_plugins(None).plugin_names == ()  # installed but disabled

configured = {"example-tools": {
    "distribution": "context-garden-example-plugin", "version": "0.1.0",
    "plugin_config": {"label": "sample", "credentials": {"token": "top-secret-value"}},
}}
loaded = load_configured_plugins(configured)
assert module in sys.modules and loaded.plugin_names == ("example-tools",)
before, identity = write_lock(root, loaded)
assert before is None and identity["plugins"][0]["distribution_version"] == "0.1.0"
assert "top-secret-value" not in (root / "garden.lock").read_text()
_, status = inspect_lock(root, configured)
assert status.valid

python_check = run_check_provider(
    loaded, "example-tools/python-check", revision="abc123", context={"label": "sample"}
)
command_check = run_check_provider(
    loaded, "example-tools/command-check", revision="abc123", context={"label": "sample"}
)
assert python_check.evidence["mode"] == "python"
assert command_check.evidence["mode"] == "json-lines"
assert dataclasses.asdict(command_check.provenance) == {
    "plugin_name": "example-tools", "distribution_version": "0.1.0",
    "api_version": "garden.plugins/v1", "capability_name": "example-tools/command-check",
    "configuration_digest": identity["plugins"][0]["configuration_digest"],
}
report = run_doctor_checks(loaded)
assert report.admission_allowed and report.results[0].message == "Example tools are ready."

resources = PluginResources(loaded)
context = resources.read("example-tools/starter-context", audience="worker",
                         product="sample-product", public_product=True)
assert "Example worker context" in context.text and context.provenance["digest"].startswith("sha256:")
profile = resources.read("example-tools/starter-profile", audience="garden-init",
                         expected_kind="init_profile")
created, conflicts = apply_profile(root / "initialized", profile_files(profile))
assert len(created) == 2 and conflicts == []

# Missing, undeclared, incompatible, duplicate, malformed, redaction, and unsafe diagnostics.
try:
    load_configured_plugins({"missing": {"distribution": "missing-dist", "version": "1.0"}})
except PluginConfigurationError as exc:
    assert "not installed; install missing-dist==1.0" in str(exc)
else:
    raise AssertionError("missing plugin accepted")
try:
    loaded.registry.capability("example-tools/not-declared")
except UndeclaredCapability as exc:
    assert "does not declare capability" in str(exc) and "it declares:" in str(exc)
else:
    raise AssertionError("undeclared capability accepted")
manifest = loaded.plugin("example-tools").manifest
try:
    PluginRegistry([dataclasses.replace(manifest, api_version="garden.plugins/v99")])
except IncompatiblePlugin as exc:
    assert "targets plugin API" in str(exc) and "this core supports" in str(exc)
else:
    raise AssertionError("incompatible plugin accepted")
duplicate = dataclasses.replace(manifest, distribution="other-example-plugin")
try:
    PluginRegistry([manifest, duplicate])
except PluginError as exc:
    assert "context-garden-example-plugin==0.1.0" in str(exc)
    assert "other-example-plugin==0.1.0" in str(exc)
else:
    raise AssertionError("duplicate plugin accepted")
try:
    run_check_provider(loaded, "example-tools/command-check", revision="abc123",
                       context={"label": "malformed"})
except PluginError as exc:
    assert "malformed JSON Lines" in str(exc)
else:
    raise AssertionError("malformed protocol accepted")
bad_config = {"example-tools": {
    "distribution": "context-garden-example-plugin", "version": "0.1.0",
    "plugin_config": {"label": 4, "credentials": {"token": "never-print-this"}},
}}
try:
    load_configured_plugins(bad_config)
except PluginConfigurationError as exc:
    assert "never-print-this" not in str(exc) and "plugins.example-tools.plugin_config.label" in str(exc)
else:
    raise AssertionError("invalid configuration accepted")
unsafe_resource = dataclasses.replace(manifest.resources[0], public_safe=False)
unsafe_manifest = dataclasses.replace(manifest, resources=(unsafe_resource, manifest.resources[1]))
unsafe_plugin = dataclasses.replace(loaded.plugin("example-tools"), manifest=unsafe_manifest)
try:
    PluginResources(LoadedPlugins((unsafe_plugin,))).read(
        "example-tools/starter-context", audience="worker", product="sample-product",
        public_product=True,
    )
except PluginError as exc:
    assert "lacks required declarations: public_safe" in str(exc)
else:
    raise AssertionError("unsafe public resource accepted")

# Fingerprint drift holds execution. Reinstalling the saved core and plugin artifacts and
# restoring their matching lock proves rollback of the complete installed set.
distribution = metadata.distribution("context-garden-example-plugin")
plugin_file = next(path for path in distribution.files if str(path).endswith("/__init__.py"))
installed_file = Path(distribution.locate_file(plugin_file))
original = installed_file.read_bytes()
core_distribution = metadata.distribution("context-garden")
core_file = next(path for path in core_distribution.files if str(path) == "garden/__init__.py")
installed_core_file = Path(core_distribution.locate_file(core_file))
original_core = installed_core_file.read_bytes()
lock = (root / "garden.lock").read_bytes()
installed_file.write_bytes(original + b"\n# drift\n")
installed_core_file.write_bytes(original_core + b"\n# replaced core\n")
drifted, drift_status = inspect_lock(root, configured)
assert not drift_status.valid and "distribution_fingerprint expected" in drift_status.hold_message
try:
    run_check_provider(drifted, "example-tools/python-check", revision="abc123")
except PluginError as exc:
    assert "compatibility hold" in str(exc)
else:
    raise AssertionError("fingerprint drift permitted execution")
subprocess.run([
    sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall",
    "--find-links", sys.argv[2], "context-garden==0.4.0",
    "context-garden-example-plugin==0.1.0",
], check=True, capture_output=True, text=True)
(root / "garden.lock").write_bytes(lock)
assert installed_core_file.read_bytes() == original_core
assert installed_file.read_bytes() == original
_, restored = inspect_lock(root, configured)
assert restored.valid
'''
    result = _run(str(python), "-c", exercise, str(tmp_path / "garden"), str(wheels),
                  cwd=tmp_path, env=environment)
    assert result.stdout == ""
