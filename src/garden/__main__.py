"""Enable `python -m garden` (used when the tool re-execs itself after an upgrade)."""

from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app()
