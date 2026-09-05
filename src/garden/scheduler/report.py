"""What one tick did: reaped, polled, dispatched, transitions and errors."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TickReport:
    reaped: list[str] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.dispatched:
            parts.append(f"dispatched {', '.join(self.dispatched)}")
        if self.transitions:
            parts.append("; ".join(self.transitions))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return " | ".join(parts) if parts else "nothing to do"

    @property
    def changed(self) -> bool:
        return bool(self.dispatched or self.transitions or self.errors)
