"""Operating profiles (CG-221): one named stop — economy, balanced, fast, or a garden's own —
that sets, together, worker and review concurrency, the tier map, the review and retro
difficulty, and the observation profile from CG-219 (see observe.py), so a person turns the
whole garden's spend up or down with one control instead of four separate config edits and a
restart. Offline like observe.py: no network, no state; `Scheduler` (scheduler/budget.py)
resolves the active stop and threads it through `effective()`.
"""

from __future__ import annotations

from typing import Any

# The facets a stop may set; anything a stop omits leaves the caller's base value (garden.yaml,
# or the plain default) standing — the same convention as observe profiles (see observe.py's
# PROFILE_FIELDS).
PROFILE_FIELDS = ("workers", "reviews", "models", "review_difficulty", "retro_difficulty", "observe")

# Built-ins, named in the task brief, ordered efficient to fast. A garden may add its own
# stops or override one of these outright under `profiles:` in garden.yaml.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "economy": {
        "workers": 3, "reviews": 2,
        "models": {"easy": "claude-haiku-4-5-20251001", "medium": "claude-haiku-4-5-20251001",
                   "hard": "claude-haiku-4-5-20251001"},
        "review_difficulty": "easy", "observe": "quiet",
    },
    "balanced": {
        "workers": 5, "reviews": 3,
        "models": {"easy": "claude-sonnet-5", "medium": "claude-sonnet-5", "hard": "claude-opus-4-8"},
        "review_difficulty": "easy", "observe": "quiet",
    },
    "fast": {
        "workers": 7, "reviews": 3,
        "models": {"easy": "claude-sonnet-5", "medium": "claude-opus-4-8", "hard": "claude-opus-4-8"},
        "review_difficulty": "medium", "observe": "watch",
    },
}


def stops(cfg: Any) -> dict[str, dict[str, Any]]:
    """Every stop, builtins first (in the efficient-to-fast order above) then any a garden
    defines or overrides under `profiles:` — a garden's own entry with the same name as a
    built-in replaces it outright, like `observe.profiles` does for observe profiles."""
    out = dict(BUILTIN_PROFILES)
    out.update({k: v for k, v in (cfg.get("profiles") or {}).items() if isinstance(v, dict)})
    return out


def describe(stop: dict[str, Any]) -> str:
    """One line of what a stop means, for the rail and the Config page: workers, reviews,
    the tier map, the review tier, the observe profile — whichever fields the stop sets."""
    bits: list[str] = []
    if "workers" in stop:
        bits.append(f"{stop['workers']} workers")
    if "reviews" in stop:
        bits.append(f"{stop['reviews']} reviews")
    models = stop.get("models") or {}
    if models:
        bits.append(", ".join(f"{tier}={model}" for tier, model in models.items()))
    if stop.get("review_difficulty"):
        bits.append(f"review {stop['review_difficulty']}")
    if stop.get("retro_difficulty"):
        bits.append(f"retro {stop['retro_difficulty']}")
    if stop.get("observe"):
        bits.append(f"feed {stop['observe']}")
    return " · ".join(bits)
