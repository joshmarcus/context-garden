"""What one tick did: reaped, polled, dispatched, transitions and errors, and how long it took."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TickReport:
    reaped: list[str] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Timing: the whole pass and each named step, so a slow pass can name the step that cost
    # it (CG-182: the tick starts and reaps checks rather than running them, and reports its
    # own duration so a regression back into a blocking pass is visible).
    duration_s: float = 0.0
    steps: dict[str, float] = field(default_factory=dict)

    def record_step(self, name: str, seconds: float) -> None:
        self.steps[name] = self.steps.get(name, 0.0) + seconds

    @property
    def slowest_step(self) -> str:
        return max(self.steps, key=self.steps.get) if self.steps else ""

    def timing(self) -> str:
        """`took 3.2s (slowest: dispatch 2.1s)` — appended to the tick summary and the warning."""
        if not self.duration_s:
            return ""
        slow = self.slowest_step
        detail = f"; slowest: {slow} {self.steps[slow]:.1f}s" if slow else ""
        return f"took {self.duration_s:.1f}s{detail}"

    def summary(self) -> str:
        parts = []
        if self.dispatched:
            parts.append(f"dispatched {', '.join(self.dispatched)}")
        if self.transitions:
            parts.append("; ".join(self.transitions))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        timing = self.timing()
        if timing:
            parts.append(timing)
        return " | ".join(parts) if parts else ("nothing to do" + (f" | {timing}" if timing else ""))

    @property
    def changed(self) -> bool:
        return bool(self.dispatched or self.transitions or self.errors)
