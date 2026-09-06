"""Regression coverage for bounded fixture Git setup."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import git


@pytest.mark.skipif(not hasattr(os, "fork"), reason="descendant-pipe regression uses POSIX fork")
def test_git_timeout_kills_descendant_that_keeps_stderr_open(tmp_path, monkeypatch):
    """A Git child can exit while its helper still owns pytest's captured pipe."""
    script = tmp_path / "git"
    descendant_pid = tmp_path / "descendant.pid"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import signal\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if os.fork() == 0:\n"
        "    helper = os.fork()\n"
        "    if helper == 0:\n"
        "        Path(os.environ['DESCENDANT_PID']).write_text(str(os.getpid()))\n"
        "        os.write(2, b'helper kept stderr open\\n')\n"
        "        signal.pause()\n"
        "    def reap_helper(signum, frame):\n"
        "        os.waitpid(helper, 0)\n"
        "        os._exit(0)\n"
        "    signal.signal(signal.SIGTERM, reap_helper)\n"
        "    signal.pause()\n"
        "sys.exit(128)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("DESCENDANT_PID", str(descendant_pid))

    with pytest.raises(RuntimeError, match="timed out after 0.1s") as error:
        git("push", cwd=tmp_path, timeout=0.1)

    assert "fixture git command timed out" in str(error.value)
    assert "helper kept stderr open" in str(error.value)
    pid = int(descendant_pid.read_text())
    deadline = time.monotonic() + 1
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{pid}").exists()
