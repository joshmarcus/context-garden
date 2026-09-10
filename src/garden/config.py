"""garden.yaml loading with defaults."""

from __future__ import annotations

import fcntl
import os
import tempfile
import warnings
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .configuration import (
    CONFIG_FIELDS,
    ApplyMode,
    ConfigProvenance,
    apply_changes,
    assert_inherited_locks_unchanged,
    assert_mutation_allowed,
    product_configuration,
    resolve_value,
    revision,
    validate_configuration,
)
from .github import is_git_remote_url

CONFIG_NAME = "garden.yaml"


def _value_at(data: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _merge_source_owners(current: Any, override: Any, source: str) -> Any:
    """Mirror ``_merge`` while retaining the owner of each surviving value leaf."""
    if not isinstance(override, dict):
        return source
    owners = dict(current) if isinstance(current, dict) else {}
    for key, value in override.items():
        owners[key] = _merge_source_owners(owners.get(key), value, source)
    return owners or source


def _owner_names(owners: Any) -> set[str]:
    if isinstance(owners, dict):
        return {name for owner in owners.values() for name in _owner_names(owner)}
    return {owners} if isinstance(owners, str) else set()

# Config keys read once at startup — either when the Scheduler is constructed or when the
# watch/serve loop first computes its sleep interval — and so NOT picked up by the per-tick
# garden.yaml reload (see Store.reload_config_if_changed). Changing one needs a restart;
# everything else takes effect on the next tick. The Configuration page names both sets.
RESTART_KEYS: list[str] = [
    field.key for field in CONFIG_FIELDS.values() if field.apply == ApplyMode.RESTART
]

NO_LIVE_GARDEN = "no-live-garden"  # subdirectory name used to build a GARDEN_ROOT that can't resolve


def no_live_garden_root(base: Path) -> str:
    """A path under `base` guaranteed not to contain a garden.yaml, for GARDEN_ROOT in
    worker and check subprocess environments (see find_root)."""
    return str(base / NO_LIVE_GARDEN)


def _normalize_review_count_policy(data: dict[str, Any]) -> None:
    """Keep legacy review-count settings loadable without letting them add a gate."""
    locations = [("github.automerge_min_review_rounds", data.get("github"))]
    locations.extend(
        (f"products.{name}.automerge_min_review_rounds", product)
        for name, product in (data.get("products") or {}).items()
        if isinstance(product, dict)
    )
    for dotted, parent in locations:
        if not isinstance(parent, dict) or "automerge_min_review_rounds" not in parent:
            continue
        value = parent["automerge_min_review_rounds"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{dotted} must be an integer")
        if value > 1:
            warnings.warn(
                f"{dotted}={value} is deprecated and has been normalized to 1; "
                "Garden requires at most one current-head approval",
                UserWarning,
                stacklevel=3,
            )
            parent["automerge_min_review_rounds"] = 1


# The fields whose value shapes what a dispatched run, its setup command or a check
# subprocess is allowed to execute. Two raw config mappings whose executable_signature()
# agree can never differ in what a worker or check can run, however many other keys (budgets,
# review settings, observe cadence, ...) changed between them. The scheduler's live-reload
# gate (Scheduler._reload_config_if_safe) holds a change here against an in-flight run's own
# fence manifest until the run is reaped or an operator confirms it (CG-242): live reload must
# never hand a worker's own garden.yaml write a route to execute before the fence (at reap)
# can revert it.
EXECUTABLE_KEYS: tuple[str, ...] = (
    "notify.command", "notify.recipient", "notify.destinations", "checks", "worker_env.pass",
    "worker_env.config_files", "sandbox", "runner_adapters", "workload_identity",
)


def executable_signature(data: dict[str, Any]) -> dict[str, Any]:
    """The values of `EXECUTABLE_KEYS`, plus every product's `setup.command` and every
    harness's `bin`/`command`, read from a raw config mapping (`Config.data`)."""

    def _get(dotted: str) -> Any:
        cur: Any = data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    sig = {key: _get(key) for key in EXECUTABLE_KEYS}
    sig["setup.command"] = {
        name: (p.get("setup") or {}).get("command")
        for name, p in (data.get("products") or {}).items()
        if isinstance(p, dict) and (p.get("setup") or {}).get("command")
    }
    sig["validation"] = {
        name: deepcopy(p.get("validation"))
        for name, p in (data.get("products") or {}).items()
        if isinstance(p, dict) and p.get("validation") is not None
    }
    sig["harnesses"] = {
        name: {"bin": h.get("bin"), "command": h.get("command")}
        for name, h in (data.get("harnesses") or {}).items() if isinstance(h, dict)
    }
    return sig


def executable_diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """The `executable_signature` keys that differ between two raw config mappings, sorted."""
    a, b = executable_signature(old), executable_signature(new)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def apply_executable_signature(data: dict[str, Any], signature: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with its executable fields restored from ``signature``.

    A fresh scheduler can load garden.yaml after an in-flight worker has changed it.  Its
    fence records this small, dispatch-time signature, which lets the scheduler retain the
    current non-executable settings while it holds the unsafe executable values for reap.
    """
    out = deepcopy(data)

    def _set(dotted: str, value: Any) -> None:
        cur: dict[str, Any] = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = cur.get(part)
            if not isinstance(child, dict):
                child = {}
                cur[part] = child
            cur = child
        cur[parts[-1]] = deepcopy(value)

    for key in EXECUTABLE_KEYS:
        _set(key, signature.get(key))

    products = out.setdefault("products", {})
    if isinstance(products, dict):
        commands = signature.get("setup.command") or {}
        validations = signature.get("validation") or {}
        for name, product in products.items():
            if not isinstance(product, dict):
                continue
            setup = product.get("setup")
            if not isinstance(setup, dict):
                setup = {}
                product["setup"] = setup
            if name in commands:
                setup["command"] = deepcopy(commands[name])
            else:
                setup.pop("command", None)
            if name in validations:
                product["validation"] = deepcopy(validations[name])
            else:
                product.pop("validation", None)

    harnesses = out.setdefault("harnesses", {})
    if isinstance(harnesses, dict):
        saved = signature.get("harnesses") or {}
        for name in set(harnesses) | set(saved):
            harness = harnesses.get(name)
            if not isinstance(harness, dict):
                harness = {}
                harnesses[name] = harness
            prior = saved.get(name) or {}
            for key in ("bin", "command"):
                if prior.get(key) is None:
                    harness.pop(key, None)
                else:
                    harness[key] = deepcopy(prior[key])
    return out


DEFAULTS: dict[str, Any] = {
    "work_dir": "",               # product clones and worktrees; empty = .garden (see Config.work_dir)
    "worktrees": {"keep_days": 2}, # prune terminal-task worktrees after this age
    "storage_cleanup": {
        "limit": 20,                # maximum trees/caches removed by one incremental sweep
        "home_keep_days": 2,        # retain recently used isolated worker homes
        "audit_keep": 20,           # bounded durable sweep receipts
        "inventory_limit": 2000,    # maximum owned paths classified by one pass
    },
    "doctor": {"min_free_mb": 2048},
    "name": "garden",
    "principles_digest": "principles/00-index.md",
    "principles_dir": "principles",
    "runner": "local",
    # Private runner classes are named only by operator configuration.  Their modules are
    # deliberately not imported while config is being read; resolution happens at runner
    # construction, after normal scheduler admission and fencing have already applied.
    "runner_adapters": {},
    # Host-trusted provider adapters and logical, least-privilege delivery policies.
    # Authority values are resolved only by operation-boundary code and never enter Config.
    "workload_identity": {"providers": {}, "references": {}, "boundaries": {}},
    "harness": "claude",
    "max_parallel": 10,
    "review_parallel": None,      # concurrent review/persona/comparison runs; None = same as max_parallel
    "resources": {               # host-wide local admission; thresholds of 0 disable sensing
        "max_parallel": None,     # capacity units shared by workers + reviews + checks
        "weight": 1,              # default reservation per run, in capacity units
        "max_bypasses": 3,        # cheap claims allowed past an older heavy run before reserving room
        "heavy_test_parallel": 1, # per-user supported setup/check/validation capacity
        # A detached check can wait for the host-wide heavy-validation lease without looking
        # like a silent worker.  This is deliberately separate from idle_kill_minutes: the
        # supervisor reports admission state, while idle detection reports missing progress.
        "admission_wait_minutes": 30,
        "min_memory_available_mb": 0,
        "min_temp_free_mb": 0,
        "execution_cgroup": "",   # delegated cgroup directory for local run descendants
        "reclaim_max_mb": 512,     # bounded best-effort file-cache reclaim; 0 disables it
        "reclaim_timeout_seconds": 5,
        "reclaim_cooldown_seconds": 300,
    },
    "max_attempts": 2,
    "max_consecutive_env_errors": 3,
    "max_revisions": 3,
    "revision_policy": {
        "enabled": True,
        "every": 2,
        "decision_after": 6,
    },
    "timeout_minutes": 90,
    "idle_minutes": 10,           # warn: show "idle N min" once a running worker has gone this long with no output or file change
    "idle_kill_minutes": 20,      # stop: past this a silent worker is killed and handled like a timeout (retry or fail); 0 disables
    "tick_interval": 60,
    "auto_revise": True,
    "auto_dispatch": True,
    "recovery": {
        # An operator may spend one additional, scheduler-owned recovery round for a
        # stopped revision or check.  It is deliberately opt-in: this is operational
        # authority, not permission to change a product outcome.
        "delegated": False,
    },
    "upgrade": "manual",          # "auto" upgrades the pinned tool install on the next idle tick;
                                  # may also be a mapping {auto, package, pip} (see Config.upgrade_*)
    "plan": {"auto_approve": True},
    "stack": True,                # start tasks on top of a dependency's open PR branch
    "discovered": {"auto_approve_blocking": True},  # blocking discovered work is created ready
    "stall": {"enabled": True},   # escalate to a human when revise rounds stop changing the diff
    "budgets": {},                # "<product>/<phase>": usd cap; also products.<name>.budget_usd
    # One hard execution budget for worker-issued validations and detached checks. Admission
    # waiting is reported separately by run_supervisor and does not consume this clock.
    "checks": {"pre_pr": [], "ci": [], "timeout_seconds": 900},
    "review": {
        "enabled": True,
        # Temporary, owner-selected escape hatch for unavailable screenshot plumbing.
        # The UI check still records a failure; only its trusted infrastructure cause becomes
        # advisory. Visible/application/check failures remain blocking.
        "capture_infrastructure_policy": "require",  # require | advisory
        "max_rounds": 2,          # positive automated-review cap per PR; null means unlimited
        "friction_after": 4,      # positive round count that records a non-blocking loop signal; null disables it
        "max_diff_chars": 60000,  # bigger diffs are read by the reviewer from git
        "harness": "",            # empty = default harness
        "difficulty": "",         # empty = the task's difficulty tier; or easy|medium|hard; PR reviews only
        "ladder": [],              # weakest-to-strongest `harness:model` PR reviewer route
        "personas": [],           # persona reviews to run on every new PR round, e.g. [security]
        "recovery_attempts": 2,   # bounded retries for a review that never yields a verdict
        "recovery_backoff_seconds": 30,  # linear delay before each recovered review admission
    },
    "retro": {
        "auto_start": False,     # queue a phase-closing review once its declared gates pass
        "personas": [],          # empty = the configured/built-in phase personas
        "prerequisites": {},     # phase key -> phase keys which must already be closed
        "require_owner_approval": False,
        "allow_frozen": False,   # freezes are holds unless closing review explicitly opts in
        "difficulty": "hard",     # tier for persona reviews (phase and PR), the retro reconciliation and
                                  # trial comparisons; separate from review.difficulty so nobody has to
                                  # edit config before a retro
        "model": "",              # names the judge's model outright, for the default harness (see
                                  # harnesses.<h>.retro_model for a non-default harness); wins over the
                                  # tier map above so a garden can price work cheaply and still judge on
                                  # its best model without editing the hard tier
    },
    "harnesses": {},
    "models": {},               # tier -> model string, or [{harness, model, weight}] pool
    "dispatch": {"spread": "quota_aware", "quota_window_hours": 6},
    "prices": {},              # generic per-model price table (input/cached_input/cache_write/output per
                               # million tokens) any harness can draw on; see harness.DEFAULT_HARNESSES for
                               # the codex defaults and docs/codex.md for where the numbers came from
    "ssh": {"hosts": []},
    "workers": {"lease_seconds": 120, "recovery_seconds": 300, "poll_seconds": 5, "hosts": []},
    "git": {"user_name": "", "user_email": ""},  # identity written into a fresh product clone; see Scheduler.git_identity
    "brief": {
        "inline_max_chars": 24000,  # reading-list files larger than this are listed, not inlined
        "total_max_chars": 120000,
    },
    "github": {
        "use_gh": True,  # prefer the gh CLI when available, else REST with GITHUB_TOKEN
        "draft_pr": True,         # open PRs as drafts; the human's triage marks them ready for review
        "project_users": [],      # additional PR authors shown in repository observations; the
                                   # authenticated user is always included
        "reviewers": [],
        "trusted_authors": [],    # logins whose PR comments may become a worker prompt, besides the
                                  # garden's own login and `reviewers`; others are logged and ignored
        "trusted_bots": [],       # [bot] logins whose PR comments may become a worker prompt; empty by
                                  # default, so no review app is trusted until its login is named here
        "automerge": False,       # let the scheduler merge a PR once every loop gate is green (off by default)
        "automerge_method": "squash",           # squash | merge | rebase
        "automerge_require_current_base": True,  # rebase onto the latest base before merging
        "automerge_min_review_rounds": 1,        # legacy values above one are normalized to one
        "automerge_tiers": ["easy", "medium"],   # only these difficulty tiers automerge under the plain policy
        "automerge_hard_tier": True,             # also merge hard-tier PRs after the configured review
                                                 # rounds and the garden's own scratch-merge check; off to
                                                 # keep hard-tier merges by hand
    },
    "ci": {
        # github preserves the existing PR-rollup gate. worker_check accepts only durable
        # receipts written by the supervised validation runner for the current source SHA.
        "status_provider": "github",  # github | worker_check
        "required": False,
        "worker_check": {"command": ""},
    },
    "notify": {
        "command": "",            # shell command to run when a task needs a human; empty = disabled
        "timeout_seconds": 30,    # timeout for the command
        "recipient": "",          # fixed calling user included in GARDEN_NOTIFICATION_JSON; never task text
        "destinations": {},         # trusted logical destination -> typed adapter configuration
    },
    "worker_env": {
        "pass": [],               # extra environment variable names or globs a worker and its setup
                                  # command keep, on top of runner.base.PASS_ENV; everything else is
                                  # dropped. HOME is not inherited (a worker runs under an isolated
                                  # scratch home); add "HOME" here to restore the operator's, or "*"
                                  # for full inheritance.
        "config_dirs": {},        # override credential *sources*, keyed by the environment
                                  # variable the harness reads. Claude's .credentials.json and
                                  # Codex's auth.json are copied into a fresh private directory
                                  # per dispatch; custom variables pass through unchanged.
        "config_files": {},       # explicitly named {source, destination, required} files;
                                  # destinations are relative to the isolated worker HOME.
    },
    "sandbox": {
        # Opt in to fail-closed execution isolation. Agent runs use their native sandbox;
        # setup/check commands additionally require the platform wrapper argv below.
        "required": False,
        "network_destinations": [],
        "command": [],
    },
    "browser_readiness": {
        "timeout_seconds": 20,   # bounded Chromium launch before capture-required work dispatches
        "retry_seconds": 300,    # retry an unchanged failed environment at most this often
    },
    "web": {
        "trusted_origins": [],    # origins besides the server's own loopback host whose POSTs
                                  # `garden serve` accepts, e.g. [https://garden.internal] behind a
                                  # reverse proxy, or a LAN address the browser reaches it by
        "operator_token_env": "", # operator bearer token; required beyond loopback
        "worker_ingress": False,  # require it on a loopback bind published to remote workers
    },
    "observe": {
        "interval": "30m",        # `garden observe --follow`: seconds between passes (30m, 2h, ... or a bare number of seconds)
        "digest_window": "30m",   # how far back each pass's digest looks
        "events": ["question", "needs_human", "failed"],  # kinds (or aliases; see garden.observe) `--follow` streams between passes
        "stuck_after": "15m",     # a running run with no output for this long is a stuck card
        "line_width": 160,        # wrap width for the text output
        "phases": "open",         # "open", or a list of "product/phase" keys, scoping the status line's counts
        "profile": "",            # one of the built-ins (quiet, watch, debug) or a name from `profiles`; empty = the fields above as-is
        "profiles": {},           # name -> partial override of interval/digest_window/events/stuck_after/line_width/phases
    },
    "operating_profile": "",      # the active stop (economy|balanced|fast, or a name from `profiles`); empty = plain config values
    "profiles": {},               # name -> partial stop (workers, reviews, models, review_difficulty, retro_difficulty, observe);
                                  # see garden.profiles.BUILTIN_PROFILES for the built-in economy/balanced/fast stops
    "products": {},
}


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    env: str = ""
    source_documents: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path, env: str | None = None) -> Config:
        """garden.yaml, then garden.<env>.yaml (env from GARDEN_ENV, e.g. work/home), then
        garden.local.yaml (per machine, gitignored). Later files override earlier keys;
        dict values merge, lists and scalars replace."""
        import os

        env = os.environ.get("GARDEN_ENV", "") if env is None else env
        data = dict(DEFAULTS)
        sources: list[str] = []
        documents: list[tuple[str, dict[str, Any]]] = [("default", DEFAULTS)]
        for name in _source_names(env):
            p = root / name
            if p.exists():
                raw = yaml.safe_load(p.read_text()) or {}
                if not isinstance(raw, dict):
                    raise ValueError(f"{name}: top level must be a mapping")
                data = _merge(data, raw)
                sources.append(name)
                documents.append((name, raw))
        _validate_product_policies(data)
        validate_configuration(data)
        _normalize_review_count_policy(data)
        return cls(root=root, data=data, sources=sources, env=env,
                   source_documents=documents)

    def editable(self) -> Config:
        """The defaults plus the document the Configuration page can actually change."""
        path = self.root / CONFIG_NAME
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{CONFIG_NAME}: top level must be a mapping")
        documents = [("default", DEFAULTS)]
        sources: list[str] = []
        if path.exists():
            documents.append((CONFIG_NAME, raw))
            sources.append(CONFIG_NAME)
        return type(self)(self.root, _merge(dict(DEFAULTS), raw), sources, self.env,
                          documents)

    def setting_source(self, key: str, product: str | None = None) -> str:
        """Name the source documents that contribute the resolved saved value."""
        project_has_override = product is not None and key in product_configuration(
            self.data, product
        )[0]
        resolved_lock = (
            product_configuration(self.data, product)[1].get(key)
            if product is not None
            else None
        )
        policy_supplies_value = isinstance(resolved_lock, dict) and "value" in resolved_lock
        owners: Any = None
        last_present_source = "default"
        for name, document in self.source_documents:
            if policy_supplies_value:
                _, locks = product_configuration(document, product or "")
                policy = locks.get(key)
                present = isinstance(policy, dict) and "value" in policy
                value = policy.get("value") if present else None
            elif project_has_override:
                overrides, _ = product_configuration(document, product or "")
                present = key in overrides
                value = overrides.get(key)
            else:
                present, value = _value_at(document, key)
            if not present:
                continue
            owners = _merge_source_owners(owners, value, name)
            last_present_source = name

        contributing = _owner_names(owners) or {last_present_source}
        names = [name for name, _ in self.source_documents if name in contributing]
        return " + ".join(names) if names else "default"

    def source_names(self) -> list[str]:
        """The garden.yaml / garden.<env>.yaml / garden.local.yaml file names this config is
        layered from, in load order (whether or not each exists). Store watches their mtimes
        to reload on change."""
        return _source_names(self.env)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def review_max_rounds(self) -> int | None:
        """The optional hard automated-review cap.

        Existing gardens retain the default of two rounds.  ``null`` deliberately means no
        cap; zero is rejected rather than becoming an ambiguous synonym for either setting.
        """
        return self._positive_optional_int("review.max_rounds")

    def review_friction_after(self) -> int | None:
        """The optional, non-blocking round count at which loop friction is recorded."""
        return self._positive_optional_int("review.friction_after")

    def capture_infrastructure_policy(self) -> str:
        """Whether trusted screenshot-infrastructure failures block visual review."""
        value = str(self.get("review.capture_infrastructure_policy", "require") or "require")
        if value not in ("require", "advisory"):
            raise ValueError("review.capture_infrastructure_policy must be 'require' or 'advisory'")
        return value

    def revision_policy(self) -> dict[str, int | bool]:
        """Validated live policy for escalating substantive implementation revisions."""
        enabled = self.get("revision_policy.enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("revision_policy.enabled must be true or false")
        every = self.get("revision_policy.every", 2)
        decision_after = self.get("revision_policy.decision_after", 6)
        for key, value in (("every", every), ("decision_after", decision_after)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"revision_policy.{key} must be a positive integer")
        if decision_after < every:
            raise ValueError("revision_policy.decision_after must be at least revision_policy.every")
        return {"enabled": enabled, "every": every, "decision_after": decision_after}

    def _positive_optional_int(self, dotted: str) -> int | None:
        value = self.get(dotted)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{dotted} must be null or a positive integer")
        return value

    def product(self, name: str) -> dict[str, Any]:
        return dict(self.data.get("products", {}).get(name, {}) or {})

    def setting(self, key: str, product: str | None = None) -> ConfigProvenance:
        """A configuration value with its global/project/policy provenance."""
        return resolve_value(self.data, key, product)

    def save_changes(self, changes: dict[str, Any], *, product: str | None = None,
                     expected_revision: str | None = None, reset: bool = False) -> Config:
        """Atomically update known settings in ``garden.yaml`` and return the reloaded config.

        The optimistic token describes the fully layered document the caller read. Only the
        base file is changed; environment and local overlays retain their existing precedence.
        Validation is performed against the resulting layered document before replacement.
        """
        lock_path = self.root / ".garden" / "config-edit.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            current = type(self).load(self.root, self.env)
            return current._save_changes_locked(
                changes, product=product, expected_revision=expected_revision, reset=reset,
            )

    def _save_changes_locked(self, changes: dict[str, Any], *, product: str | None,
                             expected_revision: str | None, reset: bool) -> Config:
        if expected_revision is not None and expected_revision != revision(self.data):
            raise RuntimeError("configuration changed since it was read; reload and try again")
        path = self.root / CONFIG_NAME
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError(f"{CONFIG_NAME}: top level must be a mapping")
        for key in changes:
            assert_mutation_allowed(self.data, key, product=product)
        if product is not None and product in (self.data.get("products") or {}):
            raw.setdefault("products", {}).setdefault(product, {})
        changed = apply_changes(raw, changes, product=product, reset=reset, validate=False)

        layered = _merge(dict(DEFAULTS), changed)
        for name in _source_names(self.env)[1:]:
            overlay_path = self.root / name
            if overlay_path.exists():
                overlay = yaml.safe_load(overlay_path.read_text()) or {}
                if not isinstance(overlay, dict):
                    raise ValueError(f"{name}: top level must be a mapping")
                layered = _merge(layered, overlay)
        _validate_product_policies(layered)
        validate_configuration(layered)
        assert_inherited_locks_unchanged(self.data, layered)

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{CONFIG_NAME}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                yaml.safe_dump(changed, stream, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return type(self).load(self.root, self.env)

    def product_github(self, name: str) -> dict[str, str]:
        """Return the product's explicitly scoped GitHub route.

        ``github: owner/repo`` remains the compact public-GitHub spelling. Enterprise
        products use a mapping so their web host, API base, and token source travel
        together instead of relying on ambient ``gh`` configuration.
        """
        product = self.product(name)
        source = product.get("source_control")
        if isinstance(source, dict):
            if str(source.get("provider") or "") != "github":
                return {}
            value = {
                "slug": source.get("repository"),
                "host": source.get("host") or urlparse(str(source.get("web_url") or "")).hostname,
                "api_base": source.get("api_url"),
                "token_env": source.get("credential_env"),
                "ca_bundle": source.get("ca_bundle"),
                "proxy": source.get("proxy"),
            }
        else:
            value = product.get("github")
        if isinstance(value, str):
            return {"slug": value, "host": "github.com"}
        if not isinstance(value, dict):
            return {}
        slug = str(value.get("slug") or "")
        host = str(value.get("host") or "github.com")
        if not slug:
            raise ValueError(f"products.{name}.github.slug is required when github is a mapping")
        return {
            "slug": slug,
            "host": host,
            "api_base": str(value.get("api_base") or value.get("api_url") or ""),
            "token_env": str(value.get("token_env") or ""),
            "ca_bundle": str(value.get("ca_bundle") or ""),
            "proxy": str(value.get("proxy") or ""),
        }

    def product_source_control(self, name: str) -> dict[str, str]:
        """Return a provider-neutral repository route, retaining ``github`` compatibility."""
        value = self.product(name).get("source_control")
        if value is None:
            github = self.product_github(name)
            return ({"provider": "github", "repository": github.pop("slug"), **github}
                    if github else {})
        if not isinstance(value, dict):
            raise ValueError(f"products.{name}.source_control must be a mapping")
        provider = str(value.get("provider") or "")
        repository = str(value.get("repository") or "")
        if not provider or not repository:
            raise ValueError(
                f"products.{name}.source_control requires provider and repository"
            )
        web_url = str(value.get("web_url") or "")
        api_url = str(value.get("api_url") or "")
        if not web_url or not api_url:
            raise ValueError(
                f"products.{name}.source_control requires web_url and api_url"
            )
        return {
            "provider": provider,
            "repository": repository,
            "host": str(value.get("host") or ""),
            "web_url": web_url,
            "api_base": api_url,
            "token_env": str(value.get("credential_env") or ""),
            "ca_bundle": str(value.get("ca_bundle") or ""),
            "proxy": str(value.get("proxy") or ""),
        }

    def product_project_users(self, name: str) -> list[str]:
        """Additional PR authors whose open work is relevant to this product.

        The authenticated GitHub user is always included by the provider. A product-level
        list replaces the garden-wide default, including when it is deliberately empty.
        This scope controls repository observation only; it does not trust those users'
        comments as worker instructions.
        """
        value: Any = self.get("github.project_users", [])
        product_github = self.product(name).get("github")
        if isinstance(product_github, dict) and "project_users" in product_github:
            value = product_github["project_users"]
        return [str(user).strip() for user in value]

    def product_repo(self, name: str) -> Path | str:
        """A local path (resolved against root) or a URL for the product's code repo."""
        repo = self.product(name).get("repo", ".")
        if is_git_remote_url(str(repo)):
            return str(repo)
        return (self.root / str(repo)).resolve()

    def product_self(self, name: str) -> bool:
        """True when this product's repo is the garden's own repo (config `self: true`).
        Such a product's tasks change the garden's own files (a friction document, a phase's
        goals, `garden.yaml`) and land as PRs to the garden repo like any other product. The
        `self` flag only tightens `garden doctor`: it refuses a `work_dir` inside the live
        garden, so the garden's clone and per-task worktrees never sit inside the live
        checkout, and it refuses a repo that resolves to the live garden root itself, so a
        worker edits a fresh clone, never the live garden (see docs/architecture.md)."""
        return bool(self.product(name).get("self"))

    def product_base_branch(self, name: str) -> str:
        return str(self.product(name).get("base_branch") or "main")

    def product_stack_owner(self, name: str) -> str:
        """Who may rewrite stacked branch history for a product.

        ``garden`` is the established default. ``external`` means another stack tool owns
        dependency branches, so scheduler recovery must leave their bases and heads alone.
        """
        return str(self.product(name).get("stack_owner") or "garden")

    def product_protected_paths(self, name: str) -> list[str]:
        """Additional product paths that always require a human merge."""
        return [str(pattern) for pattern in self.product(name).get("protected_paths", [])]

    def product_runner(self, name: str) -> str:
        r = str(self.product(name).get("runner") or self.get("runner"))
        return "local" if r == "claude-local" else r

    def runner_adapter(self, name: str) -> dict[str, Any] | None:
        """Return the trusted operator registration for a private runner, if any.

        Task frontmatter and worker output are intentionally not inputs here: only the
        layered operator configuration may name code to import.
        """
        adapters = self.get("runner_adapters") or {}
        if not isinstance(adapters, dict):
            raise ValueError("runner_adapters must be a mapping")
        registration = adapters.get(name)
        if registration is None:
            return None
        if not isinstance(registration, dict):
            raise ValueError(f"runner_adapters.{name} must be a mapping")
        return dict(registration)

    def product_harness(self, name: str) -> str:
        return str(self.product(name).get("harness") or self.get("harness") or "claude")

    def product_setup(self, name: str) -> dict[str, Any]:
        """How this product's working environment is prepared. All keys optional:

        - `command`: run once in a fresh worktree (re-run when it changes) before the worker,
          with `env` added — e.g. `uv sync --extra dev`, `npm ci`, a company bootstrap tool.
        - `env`: extra environment for the worker, the setup command and the pre-PR checks.
        - `test` / `lint`: the commands the brief tells the worker to run and the commands the
          default `checks.pre_pr` uses in the worktree.
        - `worker_push`: explicitly allow the worker to push its assigned branch for CI
          (default false); does not grant credentials or permission to manage PRs.
        - `timeout_seconds`: cap for the setup command (default 600).

        Nothing here assumes Python, pip, uv or a venv; a product that manages dependencies
        differently sets its own commands (or leaves the block empty)."""
        s = self.product(name).get("setup")
        return dict(s) if isinstance(s, dict) else {}

    def product_validation(self, name: str) -> dict[str, str]:
        """Return the product's merge-validation policy."""
        value = self.product(name).get("validation")
        if value is None:
            return {"provider": "legacy", "command": ""}
        if isinstance(value, str):
            return {"provider": value, "command": ""}
        if not isinstance(value, dict):
            raise ValueError(f"products.{name}.validation must be a string or mapping")
        return {"provider": str(value.get("provider") or ""),
                "command": str(value.get("command") or "")}

    def product_ci_policy(self, name: str) -> dict[str, Any]:
        """Resolve the exact-head gate from the product validation policy.

        The top-level ``ci`` block predates per-product validation and remains the
        compatibility policy for products which have not selected one explicitly.
        """
        validation = self.product_validation(name)
        provider = validation["provider"]
        if provider == "legacy":
            policy = dict(self.get("ci", {}) or {})
            policy["required"] = bool(
                policy.get("required") or self.product_setup(name).get("worker_push") is True
            )
            return policy
        if provider == "command":
            return {
                "status_provider": "worker_check",
                "required": True,
                "worker_check": {"command": validation["command"]},
            }
        return {"status_provider": "github", "required": provider in ("actions", "status")}

    def product_checkout(self, name: str) -> dict[str, Any]:
        """Opt-in checkout policy for a product.

        The default is the historical per-task linked worktree.  ``in_place`` names an
        explicitly provisioned canonical checkout and a command which is run before every
        use (unlike ``setup.command``, which is stamped and normally runs once).
        """
        value = self.product(name).get("checkout")
        return dict(value) if isinstance(value, dict) else {}
    def product_timeout_minutes(self, name: str) -> float:
        """Worker/revision wall-clock budget, inherited from the garden default.

        Check commands intentionally use ``checks.timeout_seconds`` instead.
        """
        value = self.product(name).get("timeout_minutes", self.get("timeout_minutes", 90))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"products.{name}.timeout_minutes must be a non-negative number")
        return float(value)

    def product_resource_weight(self, name: str) -> int:
        """Capacity units reserved by one run, inherited from ``resources.weight``."""
        resources = self.product(name).get("resources") or {}
        if not isinstance(resources, dict):
            raise ValueError(f"products.{name}.resources must be a mapping")
        value = resources.get("weight", self.get("resources.weight", 1))
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"products.{name}.resources.weight must be a positive integer (capacity units)")
        return value

    def harness(self, name: str):
        from .harness import DEFAULT_HARNESSES, Harness

        cfg = dict((self.data.get("harnesses") or {}).get(name) or {})
        # Prices merge per model rather than replace wholesale (unlike `models`, which is an
        # intentional full-replace tier map): this harness's own built-in defaults (codex's
        # price table), then the generic top-level `prices:` any harness can draw on (e.g. a
        # future harness reusing the codex-jsonl output format under a different name), then
        # `harnesses.<name>.prices`, each layer overriding only the models it names — so
        # editing one price in garden.yaml does not silently drop the rest of the table.
        default_prices = dict((DEFAULT_HARNESSES.get(name) or {}).get("prices") or {})
        generic_prices = self.data.get("prices")
        merged = {**default_prices, **(generic_prices if isinstance(generic_prices, dict) else {}), **(cfg.get("prices") or {})}
        if merged:
            cfg["prices"] = merged
        return Harness(name, cfg)

    def harness_choices(self) -> dict[str, list[str]]:
        """harness name -> known model choices, for every harness under `harnesses:` (or
        just the default harness if none is configured) — populates trial contender pickers."""
        names = list((self.data.get("harnesses") or {}).keys()) or [str(self.get("harness") or "claude")]
        return {name: self.harness(name).known_models() for name in names}

    # ---- self-upgrade (the pinned tool install) ----------------------------
    def tool_product(self) -> str | None:
        """The product that provides the `garden` binary (config `provides_tool: true`), if any."""
        for name, p in (self.data.get("products") or {}).items():
            if isinstance(p, dict) and p.get("provides_tool"):
                return name
        return None

    def upgrade_auto(self) -> bool:
        """Whether merged tool updates are installed automatically (config `upgrade: auto`)."""
        u = self.get("upgrade")
        if isinstance(u, str):
            return u.strip().lower() == "auto"
        if isinstance(u, dict):
            return bool(u.get("auto"))
        return False

    def upgrade_package(self) -> str:
        u = self.get("upgrade")
        if isinstance(u, dict) and u.get("package"):
            return str(u["package"])
        return "context-garden"

    def upgrade_pip(self) -> list[str] | None:
        """A pip command prefix override (config `upgrade.pip`), or None for the running interpreter's pip."""
        import shlex

        u = self.get("upgrade")
        if isinstance(u, dict) and u.get("pip"):
            p = u["pip"]
            return list(p) if isinstance(p, list) else shlex.split(str(p))
        return None

    @property
    def garden_dir(self) -> Path:
        """The garden's own state: state.json, events.jsonl, runs/, trials.jsonl."""
        return self.root / ".garden"

    @property
    def work_dir(self) -> Path:
        """Where product clones and per-task worktrees live. `work_dir` in config (absolute, or
        relative to the garden root); default `.garden`, the historical location. Putting it
        outside the garden keeps a worker that walks up from its checkout away from the garden,
        its venv and its state."""
        wd = self.get("work_dir")
        if not wd:
            return self.garden_dir
        return (self.root / str(wd)).resolve()

    @property
    def repos_dir(self) -> Path:
        return self.work_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.work_dir / "worktrees"

    def worktree_path(self, name: str) -> Path:
        """The worktree for `name` (a task id or a trial/phase name): the work dir, unless one
        already exists at the old location under .garden, which keeps running workers valid
        when work_dir changes."""
        new = self.worktrees_dir / name
        old = self.garden_dir / "worktrees" / name
        if new != old and old.exists() and not new.exists():
            return old
        return new


def _source_names(env: str) -> list[str]:
    return [CONFIG_NAME] + ([f"garden.{env}.yaml"] if env else []) + ["garden.local.yaml"]


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _validate_product_policies(data: dict[str, Any]) -> None:
    """Reject ambiguous branch-ownership and protected-path configuration early."""
    branches = data.get("branches") or {}
    if not isinstance(branches, dict):
        raise ValueError("branches must be a mapping")
    remote = branches.get("remote", "origin")
    if not isinstance(remote, str) or not remote.strip():
        raise ValueError("branches.remote must be a non-empty remote name")
    protected_branches = branches.get("protected", [])
    if (not isinstance(protected_branches, list)
            or any(not isinstance(branch, str) or not branch.strip() for branch in protected_branches)):
        raise ValueError("branches.protected must be a list of non-empty branch names")
    cleanup_limit = branches.get("cleanup_limit", 20)
    if isinstance(cleanup_limit, bool) or not isinstance(cleanup_limit, int) or cleanup_limit < 0:
        raise ValueError("branches.cleanup_limit must be a non-negative integer")
    storage = data.get("storage_cleanup") or {}
    if not isinstance(storage, dict):
        raise ValueError("storage_cleanup must be a mapping")
    for key in ("limit", "audit_keep", "inventory_limit", "home_keep_days"):
        value = storage.get(key, DEFAULTS["storage_cleanup"][key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"storage_cleanup.{key} must be a non-negative number")
        if key != "home_keep_days" and not isinstance(value, int):
            raise ValueError(f"storage_cleanup.{key} must be a non-negative integer")
    review = data.get("review") or {}
    if not isinstance(review, dict):
        raise ValueError("review must be a mapping")
    capture_policy = review.get("capture_infrastructure_policy", "require")
    if capture_policy not in ("require", "advisory"):
        raise ValueError("review.capture_infrastructure_policy must be 'require' or 'advisory'")
    github = data.get("github") or {}
    if not isinstance(github, dict):
        raise ValueError("github must be a mapping")
    _validate_project_users(github.get("project_users", []), "github.project_users")
    products = data.get("products") or {}
    if not isinstance(products, dict):
        raise ValueError("products must be a mapping")
    for name, product in products.items():
        if not isinstance(product, dict):
            raise ValueError(f"products.{name} must be a mapping")
        owner = product.get("stack_owner", "garden")
        if owner not in ("garden", "external"):
            raise ValueError(f"products.{name}.stack_owner must be 'garden' or 'external'")
        paths = product.get("protected_paths", [])
        if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError(f"products.{name}.protected_paths must be a list of non-empty patterns")
        product_github = product.get("github")
        if isinstance(product_github, dict) and "project_users" in product_github:
            _validate_project_users(
                product_github["project_users"], f"products.{name}.github.project_users"
            )
        validation = product.get("validation")
        if validation is not None:
            if isinstance(validation, str):
                provider, validation_command = validation, ""
            elif isinstance(validation, dict):
                provider = validation.get("provider")
                validation_command = validation.get("command", "")
            else:
                raise ValueError(f"products.{name}.validation must be a string or mapping")
            if provider not in ("actions", "status", "command", "none"):
                raise ValueError(
                    f"products.{name}.validation.provider must be 'actions', 'status', 'command', or 'none'"
                )
            if provider == "command" and (not isinstance(validation_command, str) or not validation_command.strip()):
                raise ValueError(f"products.{name}.validation.command is required for the command provider")
            if provider != "command" and validation_command:
                raise ValueError(f"products.{name}.validation.command is only valid with the command provider")


def _validate_project_users(value: Any, dotted: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(user, str) or not user.strip() for user in value
    ):
        raise ValueError(f"{dotted} must be a list of non-empty GitHub logins")


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) until a garden.yaml is found.

    Refuses to return a root whose .garden/ directory contains the starting path,
    so code running inside a worktree (.garden/worktrees/<id>) cannot act on the
    enclosing live garden. GARDEN_ROOT is a guard only, not a redirect: if it is set
    and does not contain a garden.yaml, the function raises with a message explaining
    that workers must not run garden commands. If it is set and does point at a real
    garden, it is ignored and the normal cwd walk still runs (see no_live_garden_root,
    which is how workers and check subprocesses get a GARDEN_ROOT that always trips
    this guard).
    """
    import os

    env_root = os.environ.get("GARDEN_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if not (p / CONFIG_NAME).exists():
            raise FileNotFoundError(
                f"GARDEN_ROOT={env_root!r} does not contain {CONFIG_NAME}; "
                "workers must not run garden commands against the live garden"
            )
        # GARDEN_ROOT points to a real garden. It is not a supported way to redirect the
        # root (workers and check subprocesses only ever see it set to a sentinel that
        # does not exist; see no_live_garden_root). Do NOT use it as the root: fall through
        # to the normal cwd walk so that tests running inside a subprocess find their own
        # temp garden, not the live one.

    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / CONFIG_NAME).exists():
            # Refuse if the starting path is inside this candidate's .garden/ tree.
            try:
                cur.relative_to(candidate / ".garden")
                raise FileNotFoundError(
                    f"refusing to use {candidate} as the garden root: "
                    f"{cur} is inside its .garden/ directory — "
                    "workers must not act on the enclosing live garden"
                )
            except ValueError:
                return candidate
    raise FileNotFoundError(
        f"no {CONFIG_NAME} found in {cur} or its parents (run `garden init` to create one)"
    )
