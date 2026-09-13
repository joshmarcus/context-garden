"""Operating profiles: concurrency multipliers over a garden's configuration.

The built-ins never replace model, harness, observation, or resource settings. They only
scale configured worker and review concurrency: Economy is half (rounded down, with a
positive baseline kept at one), Default is unchanged, and Fast is double. Zero remains zero.
"""

from __future__ import annotations

from typing import Any

PROFILE_FIELDS = ("workers", "reviews", "models", "review_difficulty", "retro_difficulty", "observe")
PROFILE_KEYS: dict[str, str] = {
    "max_parallel": "workers", "review_parallel": "reviews", "models": "models",
    "review.difficulty": "review_difficulty", "retro.difficulty": "retro_difficulty",
    "observe.profile": "observe",
}

# Custom definitions with these names deliberately replace the built-in entry.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "economy": {"multiplier": 0.5},
    "default": {"multiplier": 1.0},
    "fast": {"multiplier": 2.0},
}
LEGACY_DEFAULT_NAMES = frozenset(("", "plain", "balanced"))


def stops(cfg: Any) -> dict[str, dict[str, Any]]:
    """Built-ins followed by a garden's custom profiles, which may replace a built-in."""
    out = dict(BUILTIN_PROFILES)
    out.update({k: v for k, v in (cfg.get("profiles") or {}).items() if isinstance(v, dict)})
    return out


def normalized_name(cfg: Any, name: str | None) -> str:
    """Map legacy empty/plain/balanced selections to Default unless custom-defined."""
    raw = (name or "").strip()
    if raw in LEGACY_DEFAULT_NAMES and raw not in (cfg.get("profiles") or {}):
        return "default"
    return raw


def scaled_concurrency(value: Any, multiplier: float) -> int:
    """Scale deterministically: half rounds down, positive one stays one, zero stays zero."""
    baseline = int(value or 0)
    if baseline <= 0:
        return 0
    return max(1, int(baseline * multiplier))


def resolve(cfg: Any, name: str | None) -> dict[str, Any]:
    """Resolve a profile's effective facets against this configuration."""
    stop = dict(stops(cfg).get(normalized_name(cfg, name)) or {})
    multiplier = stop.pop("multiplier", None)
    if multiplier is None:
        return stop
    workers = cfg.get("max_parallel", 10)
    reviews = cfg.get("review_parallel")
    stop["workers"] = scaled_concurrency(workers, float(multiplier))
    stop["reviews"] = (scaled_concurrency(reviews, float(multiplier))
                       if reviews not in (None, "") else stop["workers"])
    return stop


def describe(stop: dict[str, Any]) -> str:
    """A short human-readable description for a profile control."""
    multiplier = stop.get("multiplier")
    if multiplier is not None:
        return f"{multiplier:g}× configured worker and review concurrency"
    bits: list[str] = []
    for key, label in (("workers", "workers"), ("reviews", "reviews")):
        if key in stop:
            bits.append(f"{stop[key]} {label}")
    for key, label in (("review_difficulty", "review"), ("retro_difficulty", "retro"), ("observe", "feed")):
        if stop.get(key):
            bits.append(f"{label} {stop[key]}")
    return " · ".join(bits)
