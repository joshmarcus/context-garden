#!/usr/bin/env python3
"""Verify one context-garden distribution through a clean, non-editable install."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def clean_environment() -> dict[str, str]:
    """Return the host environment without Python source-path overrides."""
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def run(*args: str, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, check=True, cwd=cwd, env=env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        parser.error(f"artifact does not exist: {artifact}")

    with tempfile.TemporaryDirectory(prefix="context-garden-install-") as directory:
        install_root = Path(directory)
        environment = clean_environment()
        venv = install_root / "venv"
        run(sys.executable, "-m", "venv", str(venv), cwd=install_root, env=environment)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        garden = scripts / ("garden.exe" if os.name == "nt" else "garden")
        run(str(python), "-m", "pip", "install", str(artifact), cwd=install_root,
            env=environment)
        check = """
import sys
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

import garden
from garden import __version__
from garden.model import join_frontmatter, split_frontmatter

document = join_frontmatter({'id': 'CG-1', 'status': 'ready'}, '## Goal\\n\\nShip it.')
metadata, body = split_frontmatter(document)
assert __version__ == EXPECTED_VERSION
assert metadata['id'] == 'CG-1' and body.strip().endswith('Ship it.')
environment = Path(sys.prefix).resolve()
assert Path(garden.__file__).resolve().is_relative_to(environment)
installed = distribution('context-garden')
assert Path(installed.locate_file('')).resolve().is_relative_to(environment)
package = files('garden')
assert package.joinpath('web/templates/base.html').is_file()
assert package.joinpath('web/static/plates/README.md').is_file()
"""
        run(str(python), "-c", f"EXPECTED_VERSION = {args.version!r}\n{check}",
            cwd=install_root, env=environment)
        run(str(garden), "--help", cwd=install_root, env=environment)


if __name__ == "__main__":
    main()
