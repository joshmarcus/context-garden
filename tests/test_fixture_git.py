"""Regression coverage for bounded fixture Git setup."""

from __future__ import annotations

import os
import sys

import pytest

from tests.conftest import git


@pytest.mark.skipif(not hasattr(os, "fork"), reason="descendant-pipe regression uses POSIX fork")
def test_git_timeout_kills_descendant_that_keeps_stderr_open(tmp_path, monkeypatch):
    """A Git child can exit while its helper still owns pytest's captured pipe."""
    script = tmp_path / "git"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "if os.fork() == 0:\n"
        "    os.write(2, b'helper kept stderr open\\n')\n"
        "    import time\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "sys.exit(128)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    with pytest.raises(RuntimeError, match="timed out after 0.1s") as error:
        git("push", cwd=tmp_path, timeout=0.1)

    assert "fixture git command timed out" in str(error.value)
    assert "helper kept stderr open" in str(error.value)
