"""Enumerate installed plugin entry-point metadata without executing plugin code.

Discovery answers "what is installed", never "what does it do".  It reads distribution
metadata only: ``EntryPoint.load()`` is not called, no plugin module is imported, and the
returned values carry no way to import one.  A distribution whose module raises on import
is therefore still discoverable, and a broken plugin cannot break the enumeration that
would report it.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

#: The entry-point group a plugin distribution advertises its manifest under.
ENTRY_POINT_GROUP = "garden.plugins"


@dataclass(frozen=True)
class DiscoveredEntryPoint:
    """Inert metadata for one advertised entry point: strings, never a callable."""

    name: str
    value: str
    distribution: str = ""
    distribution_version: str = ""

    @property
    def identity(self) -> str:
        if not self.distribution:
            return self.name
        return f"{self.distribution}=={self.distribution_version}"


def installed_entry_points(group: str = ENTRY_POINT_GROUP) -> tuple[DiscoveredEntryPoint, ...]:
    """Every installed entry point in ``group``, in a stable order, without loading any."""
    discovered = [
        DiscoveredEntryPoint(
            name=entry.name,
            value=entry.value,
            distribution=_distribution_name(entry),
            distribution_version=_distribution_version(entry),
        )
        for entry in metadata.entry_points(group=group)
    ]
    return tuple(sorted(discovered, key=lambda item: (item.distribution, item.name, item.value)))


def _distribution_name(entry: metadata.EntryPoint) -> str:
    """The providing distribution's name, read from metadata; unknown reads as empty."""
    distribution = getattr(entry, "dist", None)
    return "" if distribution is None else str(distribution.name or "")


def _distribution_version(entry: metadata.EntryPoint) -> str:
    distribution = getattr(entry, "dist", None)
    if distribution is None:
        return ""
    try:
        return str(distribution.version or "")
    except (KeyError, metadata.PackageNotFoundError):  # metadata present but incomplete
        return ""
