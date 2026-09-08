import tomllib
from pathlib import Path

from garden import __version__


def test_runtime_version_matches_source_package_metadata():
    package = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert __version__ == package["project"]["version"]
