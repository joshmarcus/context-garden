"""garden.yaml loading with defaults."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "garden.yaml"

# Config keys read once at startup — either when the Scheduler is constructed or when the
# watch/serve loop first computes its sleep interval — and so NOT picked up by the per-tick
# garden.yaml reload (see Store.reload_config_if_changed). Changing one needs a restart;
# everything else takes effect on the next tick. The Configuration page names both sets.
RESTART_KEYS: list[str] = [
    "work_dir",        # fixes the .garden state/run/worktree/repo paths at construction
    "tick_interval",   # garden watch / serve reads it once when the loop starts
    "github.use_gh", "github.bot_logins", "github.bot_notice_patterns",
    "github.trusted_authors", "github.trusted_bots", "github.reviewers",  # baked into the GitHub client at construction
    "upgrade.package", "upgrade.pip",  # baked into the pinned-tool installer at construction
]

NO_LIVE_GARDEN = "no-live-garden"  # subdirectory name used to build a GARDEN_ROOT that can't resolve


def no_live_garden_root(base: Path) -> str:
    """A path under `base` guaranteed not to contain a garden.yaml, for GARDEN_ROOT in
    worker and check subprocess environments (see find_root)."""
    return str(base / NO_LIVE_GARDEN)


# The fields whose value shapes what a dispatched run, its setup command or a check
# subprocess is allowed to execute. Two raw config mappings whose executable_signature()
# agree can never differ in what a worker or check can run, however many other keys (budgets,
# review settings, observe cadence, ...) changed between them. The scheduler's live-reload
# gate (Scheduler._reload_config_if_safe) holds a change here against an in-flight run's own
# fence manifest until the run is reaped or an operator confirms it (CG-242): live reload must
# never hand a worker's own garden.yaml write a route to execute before the fence (at reap)
# can revert it.
EXECUTABLE_KEYS: tuple[str, ...] = ("notify.command", "checks", "worker_env.pass")


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
    "doctor": {"min_free_mb": 2048},
    "name": "garden",
    "principles_digest": "principles/00-index.md",
    "principles_dir": "principles",
    "runner": "local",
    "harness": "claude",
    "max_parallel": 10,
    "review_parallel": None,      # concurrent review/persona/comparison runs; None = same as max_parallel
    "resources": {               # host-wide local admission; thresholds of 0 disable sensing
        "max_parallel": None,     # workers + reviews + checks; None preserves the queue limits
        "min_memory_available_mb": 0,
        "min_temp_free_mb": 0,
    },
    "max_attempts": 2,
    "max_revisions": 3,
    "timeout_minutes": 90,
    "idle_minutes": 10,           # warn: show "idle N min" once a running worker has gone this long with no output or file change
    "idle_kill_minutes": 20,      # stop: past this a silent worker is killed and handled like a timeout (retry or fail); 0 disables
    "tick_interval": 60,
    "auto_revise": True,
    "auto_dispatch": True,
    "upgrade": "manual",          # "auto" upgrades the pinned tool install on the next idle tick;
                                  # may also be a mapping {auto, package, pip} (see Config.upgrade_*)
    "plan": {"auto_approve": True},
    "stack": True,                # start tasks on top of a dependency's open PR branch
    "discovered": {"auto_approve_blocking": True},  # blocking discovered work is created ready
    "stall": {"enabled": True},   # escalate to a human when revise rounds stop changing the diff
    "budgets": {},                # "<product>/<phase>": usd cap; also products.<name>.budget_usd
    "checks": {"pre_pr": [], "ci": [], "timeout_seconds": 600},
    "review": {
        "enabled": True,
        "max_rounds": 2,          # automated review rounds per PR
        "max_diff_chars": 60000,  # bigger diffs are read by the reviewer from git
        "harness": "",            # empty = default harness
        "difficulty": "",         # empty = the task's difficulty tier; or easy|medium|hard; PR reviews only
        "ladder": [],              # weakest-to-strongest `harness:model` PR reviewer route
        "personas": [],           # persona reviews to run on every new PR round, e.g. [security]
    },
    "retro": {
        "difficulty": "hard",     # tier for persona reviews (phase and PR), the retro reconciliation and
                                  # trial comparisons; separate from review.difficulty so nobody has to
                                  # edit config before a retro
        "model": "",              # names the judge's model outright, for the default harness (see
                                  # harnesses.<h>.retro_model for a non-default harness); wins over the
                                  # tier map above so a garden can price work cheaply and still judge on
                                  # its best model without editing the hard tier
    },
    "harnesses": {},
    "prices": {},              # generic per-model price table (input/cached_input/cache_write/output per
                               # million tokens) any harness can draw on; see harness.DEFAULT_HARNESSES for
                               # the codex defaults and docs/codex.md for where the numbers came from
    "ssh": {"hosts": []},
    "git": {"user_name": "", "user_email": ""},  # identity written into a fresh product clone; see Scheduler.git_identity
    "brief": {
        "inline_max_chars": 24000,  # reading-list files larger than this are listed, not inlined
        "total_max_chars": 120000,
    },
    "github": {
        "use_gh": True,  # prefer the gh CLI when available, else REST with GITHUB_TOKEN
        "draft_pr": True,         # open PRs as drafts; the human's triage marks them ready for review
        "reviewers": [],
        "trusted_authors": [],    # logins whose PR comments may become a worker prompt, besides the
                                  # garden's own login and `reviewers`; others are logged and ignored
        "trusted_bots": [],       # [bot] logins whose PR comments may become a worker prompt; empty by
                                  # default, so no review app is trusted until its login is named here
        "automerge": False,       # let the scheduler merge a PR once every loop gate is green (off by default)
        "automerge_method": "squash",           # squash | merge | rebase
        "automerge_min_review_rounds": 1,        # require at least this many automated review rounds
        "automerge_tiers": ["easy", "medium"],   # only these difficulty tiers automerge under the plain policy
        "automerge_hard_tier": True,             # also merge hard-tier PRs, after two approving review
                                                 # rounds and the garden's own scratch-merge check; off to
                                                 # keep hard-tier merges by hand
    },
    "notify": {
        "command": "",            # shell command to run when a task needs a human; empty = disabled
        "timeout_seconds": 30,    # timeout for the command
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
    },
    "web": {
        "trusted_origins": [],    # origins besides the server's own loopback host whose POSTs
                                  # `garden serve` accepts, e.g. [https://garden.internal] behind a
                                  # reverse proxy, or a LAN address the browser reaches it by
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

    @classmethod
    def load(cls, root: Path, env: str | None = None) -> Config:
        """garden.yaml, then garden.<env>.yaml (env from GARDEN_ENV, e.g. work/home), then
        garden.local.yaml (per machine, gitignored). Later files override earlier keys;
        dict values merge, lists and scalars replace."""
        import os

        env = os.environ.get("GARDEN_ENV", "") if env is None else env
        data = dict(DEFAULTS)
        sources: list[str] = []
        for name in _source_names(env):
            p = root / name
            if p.exists():
                raw = yaml.safe_load(p.read_text()) or {}
                if not isinstance(raw, dict):
                    raise ValueError(f"{name}: top level must be a mapping")
                data = _merge(data, raw)
                sources.append(name)
        return cls(root=root, data=data, sources=sources, env=env)

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

    def product(self, name: str) -> dict[str, Any]:
        return dict(self.data.get("products", {}).get(name, {}) or {})

    def product_repo(self, name: str) -> Path | str:
        """A local path (resolved against root) or a URL for the product's code repo."""
        repo = self.product(name).get("repo", ".")
        if "://" in str(repo) or str(repo).startswith("git@"):
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

    def product_runner(self, name: str) -> str:
        r = str(self.product(name).get("runner") or self.get("runner"))
        return "local" if r == "claude-local" else r

    def product_harness(self, name: str) -> str:
        return str(self.product(name).get("harness") or self.get("harness") or "claude")

    def product_setup(self, name: str) -> dict[str, Any]:
        """How this product's working environment is prepared. All keys optional:

        - `command`: run once in a fresh worktree (re-run when it changes) before the worker,
          with `env` added — e.g. `uv sync --extra dev`, `npm ci`, a company bootstrap tool.
        - `env`: extra environment for the worker, the setup command and the pre-PR checks.
        - `test` / `lint`: the commands the brief tells the worker to run and the commands the
          default `checks.pre_pr` uses in the worktree.
        - `timeout_seconds`: cap for the setup command (default 600).

        Nothing here assumes Python, pip, uv or a venv; a product that manages dependencies
        differently sets its own commands (or leaves the block empty)."""
        s = self.product(name).get("setup")
        return dict(s) if isinstance(s, dict) else {}

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
