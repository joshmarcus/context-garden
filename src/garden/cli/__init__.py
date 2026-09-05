"""`garden` command line interface.

The commands are split by family under this package (see the module map in
docs/architecture.md); each module registers its commands against the shared `app` in
`common`. Importing them here runs their `@app.command()` decorators so every command is
registered by the time `app`/`main` are used."""

from __future__ import annotations

import sys
from importlib import import_module

from .common import app

# Import the command families for their `@app.command()` side effects. The order sets the
# order `garden --help` lists commands in, so it follows the original single-file layout.
for _family in ("scaffold", "views", "state", "loop", "planning", "diagnostics"):
    import_module(f"{__name__}.{_family}")

__all__ = ["app", "main"]


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
