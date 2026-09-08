"""Runner backends. A runner starts a worker (given a brief and, for local runners, a
worktree) and later turns its raw output into a result dict. Runners never touch task files."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from ..harness import Harness
from .base import Runner, RunnerError
from .local import LocalRunner
from .manual import ManualRunner
from .remote import RemoteRunner
from .ssh import SSHRunner

REGISTRY: dict[str, type[Runner]] = {
    LocalRunner.name: LocalRunner,
    SSHRunner.name: SSHRunner,
    ManualRunner.name: ManualRunner,
    RemoteRunner.name: RemoteRunner,
    "claude-local": LocalRunner,  # backwards-compatible alias
}

# Keep this separate from REGISTRY: tests may replace a built-in implementation, but an
# operator registration must never silently take over a public runner name or its alias.
BUILTIN_NAMES = frozenset(REGISTRY)
ADAPTER_VERSION = 1
ADAPTER_CAPABILITIES = frozenset({"detached", "remote"})


def adapter_registration_problem(name: str, registration: object) -> str | None:
    """Return a read-only registration error without importing private adapter code."""
    if name in BUILTIN_NAMES:
        return f"runner adapter {name!r} cannot replace built-in runner"
    if not isinstance(registration, dict):
        return f"runner adapter {name!r} registration must be a mapping"
    path = registration.get("path")
    if not isinstance(path, str) or not path.strip():
        return f"runner adapter {name!r} needs a dotted 'path'"
    module_name, separator, class_name = path.strip().rpartition(".")
    if not separator or not module_name or not class_name:
        return f"runner adapter {name!r} path must be a dotted class path, got {path!r}"
    return None


def _adapter_class(name: str, registration: dict[str, Any]) -> type[Runner]:
    problem = adapter_registration_problem(name, registration)
    if problem:
        raise RunnerError(problem)
    path = registration["path"].strip()
    module_name, _, class_name = path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RunnerError(f"runner adapter {name!r} could not import {module_name!r}: {exc}") from exc
    try:
        candidate = getattr(module, class_name)
    except AttributeError as exc:
        raise RunnerError(f"runner adapter {name!r} could not find {class_name!r} in {module_name!r}") from exc
    if not isinstance(candidate, type) or not issubclass(candidate, Runner):
        raise RunnerError(f"runner adapter {name!r} must name a Runner subclass")
    if inspect.isabstract(candidate):
        raise RunnerError(f"runner adapter {name!r} must implement the Runner interface")
    if "adapter_version" not in candidate.__dict__:
        raise RunnerError(f"runner adapter {name!r} must declare adapter_version")
    if candidate.adapter_version != ADAPTER_VERSION:
        raise RunnerError(
            f"runner adapter {name!r} has interface version {candidate.adapter_version!r}; "
            f"expected {ADAPTER_VERSION}"
        )
    if "capabilities" not in candidate.__dict__:
        raise RunnerError(f"runner adapter {name!r} must declare capabilities")
    capabilities = candidate.capabilities
    if not isinstance(capabilities, dict) or set(capabilities) != ADAPTER_CAPABILITIES:
        raise RunnerError(
            f"runner adapter {name!r} must declare capabilities "
            "{'detached': bool, 'remote': bool}"
        )
    if any(not isinstance(value, bool) for value in capabilities.values()):
        raise RunnerError(f"runner adapter {name!r} capability values must be booleans")
    if capabilities != {"detached": candidate.detached, "remote": candidate.remote}:
        raise RunnerError(f"runner adapter {name!r} capabilities must match detached and remote")
    return candidate


def get_runner(name: str, config: dict[str, Any], harness: Harness | None = None) -> Runner:
    adapters = config.get("_runner_adapters") or {}
    if not isinstance(adapters, dict):
        raise RunnerError("runner adapters must be configured as a mapping")
    if name in adapters:
        problem = adapter_registration_problem(name, adapters[name])
        if problem:
            raise RunnerError(problem)
    if name in REGISTRY:
        return REGISTRY[name](config, harness)
    registration = adapters.get(name)
    if not isinstance(registration, dict):
        known = sorted(set(REGISTRY) | set(adapters))
        raise RunnerError(f"unknown runner {name!r}; known: {', '.join(known)}")
    runner = _adapter_class(name, registration)(config, harness)
    # Runs store their registered name.  This permits a private class to be registered under
    # a clear operator-facing alias and still resolves the same adapter at reap/review time.
    runner.name = name
    return runner


__all__ = ["Runner", "RunnerError", "get_runner", "REGISTRY", "Harness"]
