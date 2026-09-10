"""Shared metadata and project policy for editable configuration.

The web configuration editor, CLI and scheduler all consume this module.  It deliberately
describes configuration semantics without importing a presentation layer.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .profiles import PROFILE_KEYS
from .profiles import stops as profile_stops


class ConfigScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    DERIVED = "derived"


class ApplyMode(StrEnum):
    NEXT_TICK = "next_tick"
    RESTART = "restart"
    RUNTIME = "runtime"
    DERIVED = "derived"


@dataclass(frozen=True)
class ConfigField:
    key: str
    value_type: str
    default: Any
    scopes: tuple[ConfigScope, ...]
    apply: ApplyMode
    help: str
    minimum: int | float | None = None
    choices: tuple[Any, ...] = ()
    units: str = ""
    secret: bool = False

    def validate(self, value: Any) -> None:
        if value is None and self.value_type.startswith("optional_"):
            return
        expected = self.value_type.removeprefix("optional_")
        valid = {
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "list": isinstance(value, list),
            "string_or_list": isinstance(value, (str, list)),
            "mapping": isinstance(value, dict),
        }.get(expected, True)
        if not valid:
            raise ValueError(f"{self.key} must be {self.value_type.replace('_', ' ')}")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.key} must be at least {self.minimum}")
        if self.choices and value not in self.choices:
            raise ValueError(f"{self.key} must be one of {', '.join(map(str, self.choices))}")


def _field(key: str, value_type: str, default: Any, help: str, *,
           scopes: tuple[ConfigScope, ...] = (ConfigScope.GLOBAL,),
           apply: ApplyMode = ApplyMode.NEXT_TICK, minimum: int | float | None = None,
           choices: tuple[Any, ...] = (), units: str = "", secret: bool = False) -> ConfigField:
    return ConfigField(key, value_type, default, scopes, apply, help, minimum, choices, units, secret)


# Inventory of the values currently presented on Configuration.  Collection editors in
# CG-350 can build on the same entries instead of copying types/help into a template.
CONFIG_FIELDS: dict[str, ConfigField] = {f.key: f for f in (
    _field("max_parallel", "integer", 10, "Requested concurrent worker runs; host resource limits may allow fewer.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT), minimum=1),
    _field("review_parallel", "optional_integer", None, "Concurrent review runs; empty follows max_parallel.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT), minimum=1),
    _field("auto_dispatch", "boolean", True, "Automatically starts ready work when capacity is available.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT)),
    _field("phase_execution", "string", "concurrent", "Controls whether each project admits model work from all open phases or only its earliest open phase.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT), choices=("concurrent", "sequential")),
    _field("auto_revise", "boolean", True, "Automatically starts another paid worker round after review requests changes.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT)),
    _field("stack", "boolean", True, "Starts dependent work on an open dependency branch.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT)),
    _field("operating_profile", "string", "", "Selects a named bundle of worker, review, model and observation settings.", apply=ApplyMode.RUNTIME),
    _field("models", "mapping", {}, "Maps work tiers to a model or weighted worker pool; an active operating profile supplies this map unless a live override is set.", apply=ApplyMode.RUNTIME),
    _field("observe.profile", "string", "", "Selects the observation feed preset.", apply=ApplyMode.RUNTIME),
    _field("observe.interval", "string", "30m", "Time between observation passes.", units="duration"),
    _field("observe.digest_window", "string", "30m", "History included in each observation digest.", units="duration"),
    _field("observe.events", "list", ["question", "needs_human", "failed"], "Event kinds streamed between observation passes."),
    _field("observe.stuck_after", "string", "15m", "Silence duration before a running worker is reported as stuck.", units="duration"),
    _field("observe.phases", "any", "open", "Open phases or an explicit list included in observation status."),
    _field("review.enabled", "boolean", True, "Runs automated review before a pull request can proceed.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT)),
    _field("review.max_rounds", "optional_integer", 2, "Maximum paid automated review rounds; null is unlimited.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT), minimum=1),
    _field("review.friction_after", "optional_integer", 4, "Round count that records a non-blocking loop signal; null disables it.", minimum=1),
    _field("review.difficulty", "string", "", "Reviewer model tier; empty follows the task tier.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT), choices=("", "easy", "medium", "hard")),
    _field("review.ladder", "list", [], "Weakest-to-strongest reviewer route used for escalation."),
    _field("retro.difficulty", "string", "hard", "Model tier used for retros and phase persona reviews.", choices=("easy", "medium", "hard")),
    _field("retro.auto_start", "boolean", False, "Queues the native closing review when all phase gates pass."),
    _field("retro.personas", "list", [], "Roles used by automatically queued phase-closing reviews."),
    _field("retro.prerequisites", "mapping", {}, "Maps a phase key to phases which must already be closed before its closing review."),
    _field("retro.require_owner_approval", "boolean", False, "Requires closing_review_approved in phase frontmatter before auto-start."),
    _field("retro.allow_frozen", "boolean", False, "Allows automatic closing review while a phase freeze is in force."),
    _field("github.draft_pr", "boolean", True, "Opens pull requests as drafts until human triage.", scopes=(ConfigScope.GLOBAL, ConfigScope.PROJECT)),
    _field("budgets", "mapping", {}, "USD caps keyed by product or product/phase; runtime entries take precedence.", units="USD"),
    _field("resources.max_parallel", "optional_integer", None, "Host-wide capacity units shared by workers, reviews, and checks; empty follows max_parallel.", apply=ApplyMode.RUNTIME, minimum=1),
    _field("resources.weight", "integer", 1, "Default capacity units reserved by one run.", apply=ApplyMode.RUNTIME, minimum=1),
    _field("resources.max_bypasses", "integer", 3, "Cheap claims allowed past an older heavy run before reserving room.", apply=ApplyMode.RUNTIME, minimum=0),
    _field("resources.heavy_test_parallel", "integer", 1, "Per-user supported setup, check, and validation capacity; zero disables it.", apply=ApplyMode.RUNTIME, minimum=0),
    _field("resources.admission_wait_minutes", "integer", 30, "Maximum reported wait for host-wide heavy-validation admission.", apply=ApplyMode.RUNTIME, minimum=0, units="minutes"),
    _field("resources.min_memory_available_mb", "integer", 0, "Minimum available memory before local launches pause; zero disables sensing.", apply=ApplyMode.RUNTIME, minimum=0, units="MiB"),
    _field("resources.min_temp_free_mb", "integer", 0, "Minimum temporary-disk space before local launches pause; zero disables sensing.", apply=ApplyMode.RUNTIME, minimum=0, units="MiB"),
    _field("resources.execution_cgroup", "string", "", "Delegated cgroup that enforces local descendant resource isolation."),
    _field("resources.reclaim_max_mb", "integer", 512, "Maximum bounded best-effort file-cache reclaim; zero disables it.", apply=ApplyMode.RUNTIME, minimum=0, units="MiB"),
    _field("resources.reclaim_timeout_seconds", "integer", 5, "Timeout for one bounded file-cache reclaim attempt.", apply=ApplyMode.RUNTIME, minimum=0, units="seconds"),
    _field("resources.reclaim_cooldown_seconds", "integer", 300, "Cooldown between bounded file-cache reclaim attempts.", apply=ApplyMode.RUNTIME, minimum=0, units="seconds"),
    _field("work_dir", "string", "", "Directory used for clones and worktrees.", apply=ApplyMode.RESTART),
    _field("tick_interval", "integer", 60, "Seconds between scheduler passes.", apply=ApplyMode.RESTART, minimum=1, units="seconds"),
    _field("github.use_gh", "boolean", True, "Uses the gh CLI before falling back to the GitHub API.", apply=ApplyMode.RESTART),
    _field("github.bot_logins", "list", [], "Bot identities excluded from human authorship decisions.", apply=ApplyMode.RESTART),
    _field("github.bot_notice_patterns", "list", [], "Bot comment patterns treated as notices rather than review feedback.", apply=ApplyMode.RESTART),
    _field("github.trusted_authors", "list", [], "People whose GitHub feedback may be sent to a worker.", apply=ApplyMode.RESTART),
    _field("github.trusted_bots", "list", [], "Bot identities whose GitHub feedback may be sent to a worker.", apply=ApplyMode.RESTART),
    _field("github.reviewers", "list", [], "Requested GitHub reviewers.", apply=ApplyMode.RESTART),
    _field("upgrade.package", "string", "context-garden", "Package installed by the pinned-tool upgrader.", apply=ApplyMode.RESTART),
    _field("upgrade.pip", "optional_string_or_list", None, "Optional package-installer command prefix, as a shell-style string or argument list.", apply=ApplyMode.RESTART),
    _field("dispatch_paused", "boolean", False, "Current runtime dispatch state; controlled separately from YAML.", scopes=(ConfigScope.DERIVED,), apply=ApplyMode.DERIVED),
    _field("maintenance", "mapping", {}, "Current maintenance state; controlled by the maintenance workflow.", scopes=(ConfigScope.DERIVED,), apply=ApplyMode.DERIVED),
    _field("resource_status", "mapping", {}, "Measured host capacity and pressure; it is not an independent setting.", scopes=(ConfigScope.DERIVED,), apply=ApplyMode.DERIVED),
)}


@dataclass(frozen=True)
class ConfigProvenance:
    value: Any
    source: str
    locked: bool = False
    reason: str = ""
    policy_source: str = ""


def product_configuration(data: dict[str, Any], product: str) -> tuple[dict[str, Any], dict[str, Any]]:
    product_data = (data.get("products") or {}).get(product) or {}
    configuration = product_data.get("configuration") or {}
    return dict(configuration.get("overrides") or {}), dict(configuration.get("locks") or {})


def resolve_value(data: dict[str, Any], key: str, product: str | None = None) -> ConfigProvenance:
    value = _get(data, key)
    source = "global"
    if product is None:
        return ConfigProvenance(deepcopy(value), source)
    overrides, locks = product_configuration(data, product)
    if key in overrides:
        value, source = overrides[key], f"project:{product}"
    policy = locks.get(key)
    if policy is not None:
        policy = {"reason": policy} if isinstance(policy, str) else dict(policy)
        if "value" in policy:
            value, source = policy["value"], f"policy:{product}"
        return ConfigProvenance(deepcopy(value), source, True, str(policy.get("reason") or "Locked by project policy"), str(policy.get("source") or f"products.{product}.configuration.locks"))
    return ConfigProvenance(deepcopy(value), source)


def resolve_saved_effective_value(data: dict[str, Any], key: str,
                                  product: str | None = None,
                                  active_profile: str | None = None) -> Any:
    """Resolve a setting after saved operating-profile and project policy layers.

    Runtime overrides intentionally do not participate: they live in scheduler state rather
    than the saved configuration document.  The scheduler checks that additional layer at its
    mutation and reload boundaries.
    """
    value = _get(data, key)
    profile_key = PROFILE_KEYS.get(key)
    if profile_key:
        profile_name = (str(data.get("operating_profile") or "")
                        if active_profile is None else active_profile)
        profile = profile_stops(data).get(profile_name) or {}
        if profile_key in profile:
            value = profile[profile_key]
    if product is not None:
        project_value = resolve_value(data, key, product)
        if project_value.source != "global":
            return project_value.value
    return deepcopy(value)


def validate_configuration(data: dict[str, Any]) -> None:
    """Validate known values and project policy declarations."""
    for key, field in CONFIG_FIELDS.items():
        if ConfigScope.DERIVED not in field.scopes:
            field.validate(_get(data, key, deepcopy(field.default)))
    for product, product_data in (data.get("products") or {}).items():
        if not isinstance(product_data, dict):
            continue
        overrides, locks = product_configuration(data, str(product))
        for key, value in overrides.items():
            field = CONFIG_FIELDS.get(key)
            if field is None:
                raise ValueError(f"products.{product}.configuration.overrides contains unknown setting {key!r}")
            if ConfigScope.PROJECT not in field.scopes:
                raise ValueError(f"{key} is global-only and cannot be overridden by product {product}")
            field.validate(value)
        for key, raw_policy in locks.items():
            field = CONFIG_FIELDS.get(key)
            if field is None:
                raise ValueError(f"products.{product}.configuration.locks contains unknown setting {key!r}")
            if ConfigScope.PROJECT not in field.scopes:
                raise ValueError(f"{key} is global-only and cannot be locked by product {product}")
            if not isinstance(raw_policy, (str, dict)):
                raise ValueError(f"products.{product}.configuration.locks.{key} must be a reason or mapping")
            policy = {"reason": raw_policy} if isinstance(raw_policy, str) else raw_policy
            if not str(policy.get("reason") or "").strip():
                raise ValueError(f"products.{product}.configuration.locks.{key} requires a reason")
            if "value" in policy:
                field.validate(policy["value"])


def assert_inherited_locks_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Reject a prospective document that changes a plain lock's effective value.

    A lock with no enforced ``value`` freezes the value the project currently inherits. An
    explicit project override naturally keeps that value stable; this comparison is what
    protects a lock that inherits from global configuration instead.
    """
    products = set(before.get("products") or {}) | set(after.get("products") or {})
    for product in sorted(products):
        _, locks = product_configuration(before, str(product))
        _, after_locks = product_configuration(after, str(product))
        for key, raw_policy in locks.items():
            policy = {"reason": raw_policy} if isinstance(raw_policy, str) else dict(raw_policy)
            if "value" in policy:
                continue
            after_raw = after_locks.get(key)
            after_policy = ({"reason": after_raw} if isinstance(after_raw, str)
                            else dict(after_raw) if isinstance(after_raw, dict) else None)
            # Removing a policy or replacing it with an enforced value is a trusted policy
            # operation, not an indirect ordinary edit of an inherited lock.
            if after_policy is None or "value" in after_policy:
                continue
            old = resolve_saved_effective_value(before, key, str(product))
            new = resolve_saved_effective_value(after, key, str(product))
            if old != new:
                reason = str(policy.get("reason") or "Locked by project policy")
                raise PermissionError(f"{key} is locked for {product}: {reason}")


def assert_mutation_allowed(data: dict[str, Any], key: str, *, product: str | None = None,
                            trusted_policy: bool = False) -> None:
    field = CONFIG_FIELDS.get(key)
    if field is None:
        raise ValueError(f"unknown editable setting {key!r}")
    if ConfigScope.DERIVED in field.scopes:
        raise ValueError(f"{key} is derived and cannot be edited")
    if product is not None and ConfigScope.PROJECT not in field.scopes:
        raise ValueError(f"{key} is global-only")
    if product is not None:
        if product not in (data.get("products") or {}):
            raise ValueError(f"unknown product {product!r}")
        _, locks = product_configuration(data, product)
        if key in locks and not trusted_policy:
            provenance = resolve_value(data, key, product)
            raise PermissionError(f"{key} is locked for {product}: {provenance.reason} ({provenance.policy_source})")


def revision(data: dict[str, Any]) -> str:
    """Stable optimistic-concurrency token for a persisted configuration document.

    The notification ledger location is scheduler-owned runtime metadata.  It is never
    loaded from or saved to ``garden.yaml`` and therefore must not make an unchanged
    document look stale to the shared CLI/web mutation boundary.
    """
    persisted = {key: value for key, value in data.items()
                 if key != "_notification_delivery_path"}
    encoded = json.dumps(persisted, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def apply_changes(data: dict[str, Any], changes: dict[str, Any], *, product: str | None = None,
                  expected_revision: str | None = None, reset: bool = False,
                  validate: bool = True) -> dict[str, Any]:
    """Return an atomically validated copy containing an ordinary configuration edit.

    Project resets remove overrides so inheritance resumes.  Locks live outside this mutation
    vocabulary, hence an ordinary edit can never remove its own policy.
    """
    if expected_revision is not None and expected_revision != revision(data):
        raise RuntimeError("configuration changed since it was read; reload and try again")
    candidate = deepcopy(data)
    for key, value in changes.items():
        assert_mutation_allowed(candidate, key, product=product)
        field = CONFIG_FIELDS[key]
        if not reset:
            field.validate(value)
        if product is None:
            _assign(candidate, key, value, remove=reset)
        else:
            configuration = candidate.setdefault("products", {}).setdefault(product, {}).setdefault("configuration", {})
            overrides = configuration.setdefault("overrides", {})
            if reset:
                overrides.pop(key, None)
            else:
                overrides[key] = deepcopy(value)
    if validate:
        validate_configuration(candidate)
    assert_inherited_locks_unchanged(data, candidate)
    return candidate


def audit_value(key: str, value: Any) -> Any:
    """Return a value safe for audit events and logs."""
    field = CONFIG_FIELDS.get(key)
    lowered = key.lower()
    if (field and field.secret) or any(word in lowered for word in ("secret", "token", "password", "credential", "api_key")):
        return "<redacted>"
    return deepcopy(value)


def _assign(data: dict[str, Any], dotted: str, value: Any, *, remove: bool) -> None:
    current = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    if remove:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = deepcopy(value)


def _get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
