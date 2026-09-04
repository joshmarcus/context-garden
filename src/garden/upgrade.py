"""Upgrading the garden's own pinned tool install.

When the garden manages the product that provides its `garden` binary (a product with
`provides_tool: true`), the tool is installed from a pinned commit, e.g.

    pip install "context-garden @ git+https://github.com/joshmarcus/context-garden@<sha>"

pip records the installed commit in the distribution's ``direct_url.json``.  A merge into
that product moves the pin forward; :class:`Upgrader` reinstalls at the new sha, and the
scheduler verifies the installed commit before restarting the loop.  No LLM calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PACKAGE = "context-garden"


def installed_commit(package: str = DEFAULT_PACKAGE) -> str | None:
    """The git commit the tool was installed from, read from pip's ``direct_url.json``.

    Returns ``None`` when the distribution is missing, was not installed from git
    (e.g. an editable ``pip install -e .`` checkout), or the metadata is unreadable.
    """
    try:
        from importlib.metadata import Distribution

        raw = Distribution.from_name(package).read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - PackageNotFoundError or metadata read errors
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    commit = (data.get("vcs_info") or {}).get("commit_id")
    return str(commit) if commit else None


def git_ref(url: str) -> str:
    """A pip-installable ``git+…`` VCS ref for ``url`` (a remote URL or a local path)."""
    url = str(url)
    if "://" in url or url.startswith("git@") or url.startswith("git+"):
        return url if url.startswith("git+") else f"git+{url}"
    return f"git+file://{Path(url).resolve()}"


@dataclass
class Upgrader:
    """Reinstalls the tool at a pinned commit and reports what is installed.

    Injectable so tests can supply a fake installer (see ``FakeUpgrader`` in the tests);
    the real one shells out to pip and reads pip's own metadata back.
    """

    package: str = DEFAULT_PACKAGE
    pip: list[str] | None = None      # command prefix; None = the running interpreter's pip
    exec_root: Path | None = None     # where `garden doctor` runs during verification

    def _pip(self) -> list[str]:
        return list(self.pip) if self.pip else [sys.executable, "-m", "pip"]

    def installed_commit(self) -> str | None:
        return installed_commit(self.package)

    def install(self, url: str, sha: str) -> tuple[bool, str]:
        """pip install the package at ``sha`` with ``--force-reinstall --no-deps``.

        ``--no-deps`` keeps the reinstall to the package itself (dependencies do not change
        between pins); ``--force-reinstall`` defeats pip's habit of keeping the old build
        when the version number has not moved. Returns (ok, combined output)."""
        spec = f"{self.package} @ {git_ref(url)}@{sha}"
        proc = subprocess.run([*self._pip(), "install", "--force-reinstall", "--no-deps", spec],
                              capture_output=True, text=True)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()

    def doctor_ok(self) -> bool:
        """Run the freshly-installed CLI's own `garden doctor`; True on a clean exit."""
        try:
            proc = subprocess.run([sys.executable, "-m", "garden", "doctor"],
                                  cwd=str(self.exec_root) if self.exec_root else None,
                                  capture_output=True, text=True, timeout=120)
        except Exception:  # noqa: BLE001
            return False
        return proc.returncode == 0


def default_restart() -> None:  # pragma: no cover - re-execs the process
    """Re-exec the current garden process (typically `garden serve`) so the running loop
    picks up the newly installed code. Preserves the subcommand and its flags."""
    import os

    os.execv(sys.executable, [sys.executable, "-m", "garden", *sys.argv[1:]])
