#!/usr/bin/env python3
"""Verify one context-garden distribution through a clean, non-editable install."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, check=True, env=env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        parser.error(f"artifact does not exist: {artifact}")

    with tempfile.TemporaryDirectory(prefix="context-garden-install-") as directory:
        environment = Path(directory) / "venv"
        run(sys.executable, "-m", "venv", str(environment))
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        garden = scripts / ("garden.exe" if os.name == "nt" else "garden")
        run(str(python), "-m", "pip", "install", str(artifact))
        check = """
from importlib.resources import files
from garden import __version__
from garden.model import join_frontmatter, split_frontmatter

document = join_frontmatter({'id': 'CG-1', 'status': 'ready'}, '## Goal\\n\\nShip it.')
metadata, body = split_frontmatter(document)
assert __version__ == EXPECTED_VERSION
assert metadata['id'] == 'CG-1' and body.strip().endswith('Ship it.')
package = files('garden')
assert package.joinpath('web/templates/base.html').is_file()
assert package.joinpath('web/static/plates/README.md').is_file()
"""
        run(str(python), "-c", f"EXPECTED_VERSION = {args.version!r}\n{check}")
        run(str(garden), "--help")


if __name__ == "__main__":
    main()
